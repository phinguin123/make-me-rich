/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** WebSocket URL for the backend bridge.
   *  Defaults to the Nginx-proxied path (/ws) in production.
   *  In dev, set to ws://localhost:8000/ws via .env.development.local */
  readonly VITE_WS_URL?: string;
  readonly VITE_BACKEND_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
