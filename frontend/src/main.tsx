import '@fontsource-variable/inter';
import './index.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';

async function start() {
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK === '1') {
    const { installMocks } = await import('./mocks/install');
    installMocks();
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
