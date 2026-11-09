import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The clients app runs on its own port. In development it asks the POS backend (port 8001) through this proxy, so the
// browser sees one origin; in production set VITE_API_URL to the POS server's address.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5200,
    strictPort: true,
    proxy: { '/client/api': { target: process.env.POS_API || 'http://127.0.0.1:8001', changeOrigin: true } },
  },
  preview: { host: true, port: 5200, strictPort: true },
  test: { environment: 'node' },
});
