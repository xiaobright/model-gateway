/* 几个管理页读接口共用的最小刷新队列。
   同一数据域只保留一轮在途读取和一轮待补读取；每个调用者拿到的 Promise 都要等到
   自己那轮（或更晚的补读）应用完，避免保存时误把旧 GET 当成已同步。 */

export function createRefreshQueue(load, apply) {
  let pending = null;
  let running = false;
  let sequence = 0;
  const waiters = [];

  function settle(version, error, value) {
    const done = waiters.filter((item) => item.version <= version);
    for (const item of done) {
      const at = waiters.indexOf(item);
      if (at >= 0) waiters.splice(at, 1);
      if (error) item.reject(error);
      else item.resolve(value);
    }
  }

  async function pump() {
    if (running) return;
    running = true;
    try {
      while (pending) {
        const job = pending;
        pending = null;
        try {
          const value = await load(job.args, job.version);
          // If another refresh was queued while this request was in flight,
          // its snapshot is newer.  Finish the old caller but never paint the
          // stale value before the pending read applies.
          if (!pending) apply(value, job.args, job.version);
          settle(job.version, null, value);
        } catch (error) {
          // 最后一次有效值由 apply 保留；本轮调用者仍要知道这次读取失败，才能重试。
          settle(job.version, error);
        }
      }
    } finally {
      running = false;
      // 一个极窄的时序里 finally 之后才排进 pending，确保仍会启动泵。
      if (pending) pump();
    }
  }

  return function refresh(args = {}) {
    const version = ++sequence;
    pending = { args, version };
    const promise = new Promise((resolve, reject) => waiters.push({ version, resolve, reject }));
    pump();
    return promise;
  };
}
