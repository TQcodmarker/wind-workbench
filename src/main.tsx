import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './prototype.css';
import './styles.css';
import './brand.css';
createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
