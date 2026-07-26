import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built assets are copied into the FastAPI app's static/ dir and served at
// "/" (see ../Dockerfile stage 2 + ../src/main.py's StaticFiles mount) — no
// base path needed, everything is same-origin with the API.
export default defineConfig({
  plugins: [react()],
})
