import { getFirebaseIdToken } from '../lib/firebase';

export interface ApiError {
  message: string;
  status?: number;
  data?: any;
}

export class ApiClientError extends Error {
  status?: number;
  data?: any;
  constructor(message: string, status?: number, data?: any) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
    this.data = data;
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  const contentType = res.headers.get('content-type');
  const isJson = contentType && contentType.includes('application/json');

  if (!res.ok) {
    let errorData: any = null;
    if (isJson) {
      try {
        errorData = await res.json();
      } catch {
        // ignore parse error
      }
    }
    const msg = errorData?.error || errorData?.message || `API error ${res.status}: ${res.statusText}`;
    throw new ApiClientError(msg, res.status, errorData);
  }

  if (isJson) {
    return (await res.json()) as T;
  }
  return {} as T;
}

async function authenticatedHeaders(headers?: HeadersInit): Promise<Headers> {
  const merged = new Headers(headers);
  const token = await getFirebaseIdToken();
  if (token) merged.set('Authorization', `Bearer ${token}`);
  return merged;
}

export const apiClient = {
  async get<T>(url: string, headers?: HeadersInit): Promise<T> {
    const requestHeaders = await authenticatedHeaders({
      Accept: 'application/json',
      ...headers,
    });
    const res = await fetch(url, {
      method: 'GET',
      headers: requestHeaders,
    });
    return handleResponse<T>(res);
  },

  async post<T>(url: string, body?: any, headers?: HeadersInit): Promise<T> {
    const requestHeaders = await authenticatedHeaders({
      'Content-Type': 'application/json',
      Accept: 'application/json',
      ...headers,
    });
    const res = await fetch(url, {
      method: 'POST',
      headers: requestHeaders,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    return handleResponse<T>(res);
  },
};
