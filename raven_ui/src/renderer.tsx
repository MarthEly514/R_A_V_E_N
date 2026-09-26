/**
 * Loaded by Vite and run in the "renderer" context (see main.ts for the
 * main/renderer split). Mounts the React app; App.tsx owns everything from
 * here down.
 */
import { createRoot } from 'react-dom/client';
import './index.css';
import App from './App';

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root element not found in index.html');
}
createRoot(container).render(<App />);
