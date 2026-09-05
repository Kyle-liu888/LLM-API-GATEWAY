import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: './',
  server: {
    port: 3000,
    proxy: {
      '/admin': 'http://127.0.0.1:9899',
      '/v1': 'http://127.0.0.1:9899',
      '/messages': 'http://127.0.0.1:9899',
      '/health': 'http://127.0.0.1:9899',
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  }
})
