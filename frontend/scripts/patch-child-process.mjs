/**
 * 本机沙箱禁止 spawn 子进程（EPERM），而 Vite 启动时会 `exec("net use")` 探测网络驱动器，
 * 一失败就整进程崩掉（Vite 没有 catch）。
 *
 * 这里在 Vite/Vitest 加载**之前**把 `child_process.exec` 换成「回调一个错误」的实现：
 * Vite 的 `optimizeSafeRealPathSync()` 收到 error 就直接 return，退回保守的
 * `fs.realpathSync`（非 native），功能不受影响 —— 本项目在本地磁盘上，不需要网络驱动器映射。
 *
 * 用 `createRequire` 拿 CJS 对象再改，是为了让 Node 建立 `node:child_process` 的
 * ESM 命名导出快照时看到的是**改过的**函数。
 */
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const childProcess = require('node:child_process')

const makeError = (command) => {
  const error = new Error(`spawn 已被禁用（本机沙箱限制）：${String(command)}`)
  error.code = 'EPERM'
  return error
}

childProcess.exec = function patchedExec(command, options, callback) {
  const done = typeof options === 'function' ? options : callback
  if (typeof done === 'function') {
    process.nextTick(() => done(makeError(command), '', ''))
  }
  return { on() {}, once() {}, kill() {}, unref() {} }
}