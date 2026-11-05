let token = '';
export let dataDirectory = '';
export async function initialize() {
  const response = await fetch('/api/session');
  const session = await response.json();
  token = session.token;
  dataDirectory = session.data_dir;
}
export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method, headers: { 'X-Session-Token': token, ...(body instanceof FormData ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Сервер недоступен' }));
    throw new Error(error.detail || 'Ошибка запроса');
  }
  return response.json();
}
export async function download(path: string, name: string, body?: unknown) {
  const response = await fetch('/api' + path, { method: body ? 'POST' : 'GET',
    headers: { 'X-Session-Token': token, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) throw new Error((await response.json()).detail);
  const url = URL.createObjectURL(await response.blob());
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
