import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { RunStatus, type GenerationRun, type Spec } from '../types';
import { deleteSpec, formatStatus, listRuns, listSpecs, runPath, uploadSpec, uploadSpecFromUrl } from '../api';

const GATE_LABEL: Partial<Record<RunStatus, string>> = {
  [RunStatus.AWAITING_REQUIREMENTS_APPROVAL]: 'Gate 1 · Requirements',
  [RunStatus.AWAITING_CASE_APPROVAL]: 'Gate 2 · Test cases',
};

export function SpecList() {
  const navigate = useNavigate();
  const [specs, setSpecs] = useState<Spec[]>([]);
  const [runs, setRuns] = useState<GenerationRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [url, setUrl] = useState('');
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => { void load(); }, []);

  async function load() {
    try {
      const [s, r] = await Promise.all([listSpecs(), listRuns()]);
      setSpecs(s);
      setRuns(r);
      setError(null);
    } catch (e) {
      setError(`Could not load specs: ${e}`);
    } finally {
      setLoading(false);
    }
  }

  async function add(work: () => Promise<Spec>) {
    setBusy(true);
    setError(null);
    try {
      const spec = await work();
      await load();
      navigate(`/specs/${spec.id}/run`);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: number) {
    try {
      await deleteSpec(id);
      await load();
    } catch (e) {
      setError(String(e));
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) void add(() => uploadSpec(file));
  }

  if (loading) return <div className="screen">Loading specs…</div>;

  const pending = runs
    .filter(r => r.status in GATE_LABEL)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const specName = (id: number) => specs.find(s => s.id === id)?.name ?? `Spec #${id}`;

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Specifications</h1>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {pending.length > 0 && (
        <section className="tray">
          <h2 className="tray-heading">Waiting on you ({pending.length})</h2>
          <div className="tray-cards">
            {pending.map(run => (
              <button key={run.id} className="tray-card" onClick={() => navigate(runPath(run))}>
                <span className="tray-card-spec">{specName(run.spec_id)}</span>
                <span className="tray-card-gate">{GATE_LABEL[run.status]}</span>
                <span className="muted">Run #{run.id} · {run.provider}</span>
                <span className="tray-card-go">Review →</span>
              </button>
            ))}
          </div>
        </section>
      )}

      <section
        className={`dropzone ${dragging ? 'dropzone-active' : ''} ${specs.length === 0 ? 'dropzone-hero' : ''}`}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => fileInput.current?.click()}
      >
        <input ref={fileInput} type="file" accept=".json,.yaml,.yml" hidden disabled={busy}
               onChange={e => {
                 const file = e.target.files?.[0];
                 e.target.value = '';
                 if (file) void add(() => uploadSpec(file));
               }} />
        <p className="dropzone-label">
          {busy ? 'Uploading…' : 'Drop an OpenAPI 3.x file here, or click to browse'}
        </p>
        <form className="url-row" onClick={e => e.stopPropagation()}
              onSubmit={e => { e.preventDefault(); if (url) void add(() => uploadSpecFromUrl(url)); }}>
          <span className="muted">or fetch by URL</span>
          <input type="url" placeholder="https://…/openapi.json"
                 value={url} onChange={e => setUrl(e.target.value)} disabled={busy} />
          <button className="btn btn-secondary" type="submit" disabled={busy || !url}>Fetch</button>
        </form>
      </section>

      {specs.length === 0 ? null : (
        <table>
          <thead>
            <tr><th>Name</th><th>Version</th><th>Endpoints</th><th>Source</th><th>Added</th><th /></tr>
          </thead>
          <tbody>
            {specs.map(spec => {
              const specRuns = runs.filter(r => r.spec_id === spec.id);
              return (
                <React.Fragment key={spec.id}>
                  <tr>
                    <td>{spec.name}</td>
                    <td>{spec.version}</td>
                    <td>{spec.endpoint_count}</td>
                    <td className="truncate" title={spec.source_ref}>{spec.source_ref}</td>
                    <td>{new Date(spec.created_at).toLocaleDateString()}</td>
                    <td className="row-actions">
                      <button className="btn btn-sm btn-primary"
                              onClick={() => navigate(`/specs/${spec.id}/run`)}>New run</button>
                      <button className="btn btn-sm btn-ghost" onClick={() => void remove(spec.id)}>Delete</button>
                    </td>
                  </tr>
                  <tr className="detail-row">
                    <td colSpan={6}>
                      {specRuns.length === 0 ? (
                        <span className="muted">No runs yet.</span>
                      ) : (
                        <div className="run-list">
                          {specRuns.map(run => (
                            <button key={run.id} className="run-row" onClick={() => navigate(runPath(run))}>
                              <code>Run #{run.id}</code>
                              <span className="muted">{run.provider} · {run.model}</span>
                              <span className="muted run-row-date">
                                {new Date(run.created_at).toLocaleString()}
                              </span>
                              <span className={`status-pill status-${run.status}`}>
                                {formatStatus(run.status)}
                              </span>
                            </button>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
