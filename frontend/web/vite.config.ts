import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 后端默认端口需与 RESEARCHX_WEB_PORT 保持一致（默认 8010；CLI 见 src/researchx/cli.py）。
const backendTarget = 'http://127.0.0.1:8010';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: backendTarget } },
  },
});
