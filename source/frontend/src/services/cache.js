const store = new Map();

// 缓存 Promise 本身而非结果：既能复用已完成的数据，也能合并同一时刻的并发请求；
// 请求失败时自动清除条目，确保下次调用能重新发起请求。
export function apiWithCache(url, ttl, fn) {
  const cached = store.get(url);
  const now = Date.now();
  if (cached && cached.expiresAt > now) {
    return cached.promise;
  }
  const promise = fn().catch((error) => {
    store.delete(url);
    throw error;
  });
  store.set(url, { promise, expiresAt: now + ttl });
  return promise;
}

export function invalidateApiCache(url) {
  store.delete(url);
}

// 同步判断 URL 是否有未过期的缓存，用于在发起请求前决定是否跳过加载态。
export function peekApiCache(url) {
  const cached = store.get(url);
  return !!(cached && cached.expiresAt > Date.now());
}
