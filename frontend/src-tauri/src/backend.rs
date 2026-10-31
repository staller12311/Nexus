use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::Duration;

use tauri::{AppHandle, Manager};

use crate::logging;
use crate::splash;
use crate::state::BackendState;

/// Windows CREATE_NO_WINDOW：console 子系统程序不会弹出黑框。
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const PORT_PREFIX: &str = "NEXUS_PORT=";
const PORT_TIMEOUT: Duration = Duration::from_secs(45);
const HEALTH_ATTEMPTS: u32 = 450;
const MAX_START_ATTEMPTS: u32 = 2;
static START_ATTEMPTS: AtomicU32 = AtomicU32::new(0);

pub fn start(app: AppHandle) {
    let attempt = START_ATTEMPTS.fetch_add(1, Ordering::SeqCst) + 1;
    logging::log(format!(
        "backend start attempt={attempt}: production={}",
        !cfg!(debug_assertions)
    ));
    let mut command = match build_command(&app) {
        Some(command) => command,
        None => {
            startup_failed(
                &app,
                attempt,
                "找不到可启动的 backend（开发脚本或打包产物）",
            );
            return;
        }
    };
    command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUNBUFFERED", "1");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = match command.spawn() {
        Ok(child) => {
            logging::log(format!("backend spawned: pid={}", child.id()));
            child
        }
        Err(error) => {
            startup_failed(&app, attempt, &format!("启动 backend 失败：{error}"));
            return;
        }
    };

    attach_job(&app, &child);

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    app.state::<BackendState>()
        .child
        .lock()
        .expect("backend state poisoned")
        .replace(child);

    if let Some(stderr) = stderr {
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                logging::log(format!("backend stderr: {line}"));
            }
        });
    }

    let (port_tx, port_rx) = mpsc::channel::<u16>();
    if let Some(stdout) = stdout {
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Some(port) = line.strip_prefix(PORT_PREFIX) {
                    if let Ok(port) = port.trim().parse::<u16>() {
                        let _ = port_tx.send(port);
                        break;
                    }
                }
                logging::log(format!("backend stdout: {line}"));
            }
        });
    }

    let port = match port_rx.recv_timeout(PORT_TIMEOUT) {
        Ok(port) => port,
        Err(RecvTimeoutError::Timeout) => {
            startup_failed(&app, attempt, "后端 45 秒内没有报告端口");
            return;
        }
        Err(RecvTimeoutError::Disconnected) => {
            startup_failed(&app, attempt, "后端进程未报告端口就退出了");
            return;
        }
    };
    app.state::<BackendState>()
        .port
        .lock()
        .expect("backend state poisoned")
        .replace(port);

    logging::log(format!("backend port={port}, health checking"));
    if wait_health(port) {
        logging::log("backend health=200, showing main window");
        splash::show_main(&app, port);
    } else {
        startup_failed(&app, attempt, "后端健康检查超时");
    }
}

/// 启动失败：最多重试一次；最终失败时展示错误并自动退出，避免留下无法重启的进程。
fn startup_failed(app: &AppHandle, attempt: u32, message: &str) {
    logging::log(format!(
        "backend startup failed attempt={attempt}: {message}"
    ));
    stop(app);

    if attempt < MAX_START_ATTEMPTS {
        std::thread::sleep(Duration::from_millis(800));
        let handle = app.clone();
        std::thread::spawn(move || start(handle));
        return;
    }

    let full = format!("{message}；日志：{}", logging::path().display());
    splash::fail(app, &full);

    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(10));
        logging::log("exiting after startup failure");
        handle.exit(1);
    });
}

/// 应用退出时同时 kill 子进程和关闭 Job Object（后者负责进程树兜底）。
pub fn stop(app: &AppHandle) {
    logging::log("backend stop requested");
    let state = app.state::<BackendState>();
    if let Some(mut child) = state.child.lock().expect("backend state poisoned").take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::CloseHandle;
        let job = state.job.lock().expect("backend state poisoned").take();
        if let Some(job) = job {
            unsafe {
                CloseHandle(job as windows_sys::Win32::Foundation::HANDLE);
            }
        }
    }
}

fn build_command(app: &AppHandle) -> Option<Command> {
    let command = if cfg!(debug_assertions) {
        dev_command()
    } else {
        production_command(app)
    };
    if command.is_none() {
        logging::log("backend command not found");
    }
    command
}

/// dev 模式：spawn 本地 Python + packaging/nexus_backend.py（和打包后的入口同源）。
fn dev_command() -> Option<Command> {
    let root = project_root();
    let script = root.join("packaging").join("nexus_backend.py");
    if !script.is_file() {
        eprintln!("[nexus] 缺少 {}", script.display());
        return None;
    }
    let system_python = PathBuf::from(r"D:\python3.11\python.exe");
    let venv_python = root.join(".venv").join("Scripts").join("python.exe");
    let python = if system_python.is_file() {
        system_python
    } else if venv_python.is_file() {
        venv_python
    } else {
        PathBuf::from("python")
    };

    let mut command = Command::new(python);
    command
        .arg(&script)
        .arg("--config")
        .arg(root.join("config.yaml"))
        .current_dir(&root)
        .env(
            "PYTHONPATH",
            root.join(".venv").join("Lib").join("site-packages"),
        );
    Some(command)
}

/// 生产模式：Tauri resource 里的 PyInstaller 后端目录。
fn production_command(app: &AppHandle) -> Option<Command> {
    let resource_dir = app.path().resource_dir().ok()?;
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(std::path::Path::to_path_buf));
    let mut candidates = vec![
        resource_dir.join("backend").join("nexus-backend.exe"),
        resource_dir
            .join("resources")
            .join("backend")
            .join("nexus-backend.exe"),
    ];
    if let Some(exe_dir) = &exe_dir {
        candidates.push(exe_dir.join("backend").join("nexus-backend.exe"));
        candidates.push(
            exe_dir
                .join("resources")
                .join("backend")
                .join("nexus-backend.exe"),
        );
    }
    let exe = match candidates.into_iter().find(|path| path.is_file()) {
        Some(path) => path,
        None => {
            logging::log(format!(
                "backend executable not found (resource_dir={})",
                resource_dir.display()
            ));
            return None;
        }
    };
    logging::log(format!("backend executable: {}", exe.display()));
    // 允许测试/便携发行版覆盖数据目录；正常安装版仍走 %APPDATA%\<identifier>。
    let data_dir = std::env::var_os("NEXUS_DATA_DIR")
        .map(std::path::PathBuf::from)
        .or_else(|| app.path().app_data_dir().ok())?;
    std::fs::create_dir_all(&data_dir).ok()?;

    let mut command = Command::new(exe);
    command
        .arg("--data-dir")
        .arg(&data_dir)
        .current_dir(&data_dir);
    Some(command)
}

fn project_root() -> PathBuf {
    // CARGO_MANIFEST_DIR = <repo>/frontend/src-tauri
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn wait_health(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    for _ in 0..HEALTH_ATTEMPTS {
        if let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
            if stream
                .write_all(
                    b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
                )
                .is_ok()
            {
                let mut buffer = [0u8; 128];
                if let Ok(size) = stream.read(&mut buffer) {
                    let head = String::from_utf8_lossy(&buffer[..size]);
                    if head.starts_with("HTTP/1.1 200") || head.starts_with("HTTP/1.0 200") {
                        return true;
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

#[cfg(windows)]
fn attach_job(app: &AppHandle, child: &Child) {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            eprintln!("[nexus] CreateJobObjectW 失败，仅依赖 child.kill() 兜底");
            return;
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &mut info as *mut _ as *mut _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if configured != 0 && AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE) != 0 {
            app.state::<BackendState>()
                .job
                .lock()
                .expect("backend state poisoned")
                .replace(job as isize);
            return;
        }
        CloseHandle(job);
    }
}

#[cfg(not(windows))]
fn attach_job(_app: &AppHandle, _child: &Child) {}
