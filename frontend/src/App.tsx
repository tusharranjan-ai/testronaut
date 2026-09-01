import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { SpecList } from './screens/SpecList';
import { RunConfig } from './screens/RunConfig';
import { Progress } from './screens/Progress';
import { RequirementsReview } from './screens/RequirementsReview';
import { CaseReview } from './screens/CaseReview';
import { ExportImport } from './screens/ExportImport';
import { Automation } from './screens/Automation';
import './App.css';

function NotFound() {
  return (
    <div className="screen empty-state">
      <p>That page does not exist.</p>
      <Link to="/specs">Back to specs</Link>
    </div>
  );
}

// The pipeline runs through these fixed stages in this order — the stepper
// marks real position in the process, not decorative numbering.
const PIPELINE = [
  { key: 'run', label: 'Configure', test: (p: string) => p.endsWith('/run') },
  { key: 'progress', label: 'Generate', test: (p: string) => p.includes('/progress') },
  { key: 'requirements', label: 'Requirements · gate 1', test: (p: string) => p.includes('/requirements') },
  { key: 'cases', label: 'Cases · gate 2', test: (p: string) => p.includes('/cases') },
  { key: 'export', label: 'Export', test: (p: string) => p.includes('/export') },
  { key: 'automation', label: 'Automate', test: (p: string) => p.includes('/automation') },
];

function Stepper() {
  const { pathname } = useLocation();
  const activeIndex = PIPELINE.findIndex(stage => stage.test(pathname));
  if (activeIndex === -1) return null;

  return (
    <div className="stepper">
      {PIPELINE.map((stage, i) => (
        <span key={stage.key} style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
          {i > 0 && <span className="stepper-sep">—</span>}
          <span className={`stepper-stage ${i === activeIndex ? 'is-active' : i < activeIndex ? 'is-done' : ''}`}>
            {stage.label}
          </span>
        </span>
      ))}
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="app">
        <nav className="main-nav">
          <Link to="/specs" className="nav-brand">Testronaut</Link>
          <Stepper />
          <div className="nav-links"><Link to="/specs">Specs</Link></div>
        </nav>
        <main className="main-content">
          <Routes>
            <Route path="/specs" element={<SpecList />} />
            <Route path="/specs/:specId/run" element={<RunConfig />} />
            <Route path="/runs/:runId/progress" element={<Progress />} />
            <Route path="/runs/:runId/requirements" element={<RequirementsReview />} />
            <Route path="/runs/:runId/cases" element={<CaseReview />} />
            <Route path="/runs/:runId/export" element={<ExportImport />} />
            <Route path="/runs/:runId/automation" element={<Automation />} />
            <Route path="/" element={<Navigate to="/specs" replace />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
