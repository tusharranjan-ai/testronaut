import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { RunStatus, type GenerationRun, type ImportSummary, type TraceabilityReport } from '../types';
import { exportUrl, formatStatus, getRun, getTraceability, importFile } from '../api';

export function ExportImport() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const id = Number(runId);

  const [run, setRun] = useState<GenerationRun | null>(null);
  const [trace, setTrace] = useState<TraceabilityReport | null>(null);
  const [onlyApproved, setOnlyApproved] = useState(true);
  const [summary, setSummary] = useState<ImportSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([getRun(id), getTraceability(id)])
      .then(([r, t]) => { setRun(r); setTrace(t); })
      .catch(e => setError(String(e)));
  }, [id]);

  async function upload(file: File) {
    setBusy(true); setError(null); setSummary(null);
    try {
      setSummary(await importFile(id, file));
      setTrace(await getTraceability(id));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!run) return <div className="screen">{error ?? 'Loading…'}</div>;

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Run #{run.id} · Export</h1>
        <span className={`status-pill status-${run.status}`}>{formatStatus(run.status)}</span>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {run.status !== RunStatus.DONE && (
        <div className="alert alert-warn">
          This run has not passed both gates yet. You can still export what exists.
        </div>
      )}

      {trace && (
        <p className="muted">
          {trace.requirements_total} requirements · {trace.cases_total} cases ·{' '}
          {trace.covered}/{trace.total_requirements} approved requirements covered
        </p>
      )}

      <section className="config-section">
        <h2>Export</h2>
        <label className="checkbox">
          <input type="checkbox" checked={onlyApproved}
                 onChange={e => setOnlyApproved(e.target.checked)} />
          Approved artifacts only
        </label>
        <div className="row-actions">
          <a className="btn btn-primary" href={exportUrl(id, 'json', onlyApproved)}>
            Download JSON
          </a>
          <a className="btn btn-secondary" href={exportUrl(id, 'xlsx', onlyApproved)}>
            Download Excel
          </a>
        </div>
        <p className="muted">
          JSON is canonical and round-trips exactly. Excel is for reviewers to edit;
          import it back below and the requirement links survive.
        </p>
      </section>

      <section className="config-section">
        <h2>Import edits</h2>
        <label className={`btn btn-secondary ${busy ? 'disabled' : ''}`}>
          {busy ? 'Importing…' : 'Choose a .json or .xlsx file'}
          <input type="file" accept=".json,.xlsx" hidden disabled={busy}
                 onChange={e => {
                   const file = e.target.files?.[0];
                   e.target.value = '';
                   if (file) void upload(file);
                 }} />
        </label>

        {summary && (
          <div className="import-summary">
            {(['requirements', 'cases'] as const).map(kind => {
              const s = summary[kind];
              return (
                <div key={kind}>
                  <h3>{kind === 'requirements' ? 'Requirements' : 'Test cases'}</h3>
                  <p>{s.inserted} added · {s.updated} updated · {s.unchanged} unchanged</p>
                  {s.missing_from_file.length > 0 && (
                    <p className="muted">
                      In the run but not in the file (kept, never deleted):{' '}
                      {s.missing_from_file.map(x => <code key={x}>{x}</code>)}
                    </p>
                  )}
                  {s.errors.length > 0 && (
                    <ul className="import-errors">
                      {s.errors.map((err, i) => (
                        <li key={i}>row {err.row} of {err.sheet}: {err.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <div className="screen-footer">
        <button className="btn btn-ghost" onClick={() => navigate(`/runs/${id}/cases`)}>
          Back to test cases
        </button>
        <button className="btn btn-primary" onClick={() => navigate(`/runs/${id}/automation`)}>
          Generate automation →
        </button>
        <button className="btn btn-ghost" onClick={() => navigate('/specs')}>All specs</button>
      </div>
    </div>
  );
}
