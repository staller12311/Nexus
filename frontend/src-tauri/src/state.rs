use std::process::Child;
use std::sync::Mutex;

/// Tauri 进程内共享的后端句柄。
///
/// `job` 存的是 Windows Job Object 的 HANDLE（转成 isize 才能跨线程传递），
/// 应用退出时先 `child.kill()`，再关闭 job handle 兜底杀整棵进程树。
#[derive(Default)]
pub struct BackendState {
    pub child: Mutex<Option<Child>>,
    pub port: Mutex<Option<u16>>,
    pub job: Mutex<Option<isize>>,
}
