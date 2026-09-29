import { defineConfig } from 'vite';
export default defineConfig({base:'/',build:{outDir:'../gateway/static',emptyOutDir:true},server:{proxy:{'/api':'http://127.0.0.1:8000','/healthz':'http://127.0.0.1:8000'}}});
