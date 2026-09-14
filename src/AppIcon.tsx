import type {ReactNode} from 'react';
import './app-icons.css';

export type AppIconKind = 'workbench' | 'runs' | 'sources' | 'trace' | 'models' | 'assistant';

type AppIconProps = {
  kind: AppIconKind;
  className?: string;
};

const glyphs: Record<AppIconKind, ReactNode> = {
  workbench: <>
    <path className="app-icon-surface" d="M6 4h12a3 3 0 0 1 3 3v3H3V7a3 3 0 0 1 3-3Z"/>
    <path className="app-icon-accent" d="M9 15h6v5H9Z"/>
    <rect x="3" y="4" width="18" height="16" rx="3"/>
    <path d="M3 10h18M3 15h18M9 10v10M15 10v10"/>
  </>,
  runs: <>
    <circle className="app-icon-surface" cx="12" cy="12" r="8"/>
    <path d="M4.5 8A8 8 0 1 1 4 14M4 4v4h4"/>
    <path d="M12 7v5l3.5 2"/>
    <circle className="app-icon-dot" cx="12" cy="12" r="1"/>
  </>,
  sources: <>
    <rect className="app-icon-surface" x="2" y="5" width="6" height="14" rx="2"/>
    <rect className="app-icon-surface" x="16" y="5" width="6" height="14" rx="2"/>
    <rect x="2" y="5" width="6" height="14" rx="2"/>
    <rect x="16" y="5" width="6" height="14" rx="2"/>
    <path d="M8 12h8M2 15h6M16 15h6"/>
    <circle className="app-icon-dot" cx="5" cy="9" r="1"/>
    <circle className="app-icon-dot" cx="19" cy="9" r="1"/>
  </>,
  trace: <>
    <path d="M12 8v4M5 16v-4h14v4"/>
    <circle className="app-icon-accent" cx="12" cy="5" r="3"/>
    <circle className="app-icon-surface" cx="5" cy="19" r="3"/>
    <circle className="app-icon-surface" cx="19" cy="19" r="3"/>
    <circle cx="12" cy="5" r="3"/>
    <circle cx="5" cy="19" r="3"/>
    <circle cx="19" cy="19" r="3"/>
  </>,
  models: <>
    <rect className="app-icon-surface" x="7" y="7" width="10" height="10" rx="2"/>
    <path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/>
    <rect x="7" y="7" width="10" height="10" rx="2"/>
    <path className="app-icon-accent" d="m12 9 3 3-3 3-3-3Z"/>
    <circle className="app-icon-dot" cx="12" cy="12" r="1"/>
  </>,
  assistant: <>
    <path className="app-icon-surface" d="M6 4h12a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3h-6l-5 3v-3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3Z"/>
    <path d="M6 4h12a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3h-6l-5 3v-3H6a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3Z"/>
    <circle className="app-icon-dot" cx="8.5" cy="10" r="1"/>
    <circle className="app-icon-dot" cx="15.5" cy="10" r="1"/>
    <path d="M9 14h6"/>
  </>,
};

export default function AppIcon({kind, className}: AppIconProps) {
  return <svg
    className={className ? `app-icon ${className}` : 'app-icon'}
    viewBox="0 0 24 24"
    width="24"
    height="24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    focusable="false"
  >{glyphs[kind]}</svg>;
}
