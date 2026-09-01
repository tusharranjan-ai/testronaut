import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { RunStatus, type GenerationRun } from '../types';
import { formatStatus, getRun } from '../api';

interface LogLine { event: string; text: string; key: string }

const STAGE_LABEL: Record<string, string> = {
  requirements: 'Requirements',
  test_cases: 'Test cases',
};

export function Progress() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const [run, setRun] = useState<GenerationRun | null>(null);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [done, setDone] = useState({ current: 0, total: 0 });
  const [phase, setPhase] = useState<'Generating' | 'Reviewing'>('Generating');
  const [error, setError] = useState<string | null>(null);
  const counter = useRef(0);

  function log(event: string, text: string) {
    counter.current += 1;
    setLines(prev => [...prev.slice(-200), { event, text, key: `${counter.current}` }]);
  }

  useEffect(() => {
    if (!runId) return;
    const id = Number(runId);
    let closed = false;

    getRun(id).then(setRun).catch(e => setError(String(e)));

    const source = new EventSource(`/api/runs/${id}/stream`);

    const on = (name: string, handler: (data: any) => void) =>
      source.addEventListener(name, (e: MessageEvent) => {
        if (!closed) handler(JSON.parse(e.data));
      });

    on('status', d => {
      setRun(prev => (prev ? { ...prev, status: d.status, error: d.error } : prev));
      // Sent on connect and on every transition, so a client that attaches after
      // stage_start still learns the denominator instead of showing "N / ?".
      const reviewing = d.status === 'reviewing_requirements' || d.status === 'reviewing_cases';
      setDone(p => ({
        // Generation and review are two passes over the same endpoints; restart
        // the count so the bar tracks the pass actually running.
        current: reviewing ? 0 : p.current,
        total: p.total || d.total_endpoints || 0,
      }));
      if (reviewing) setPhase('Reviewing');
      else if (d.status?.startsWith('generating')) setPhase('Generating');
    });
    on('stage_start', d => {
      setDone({ current: 0, total: d.total_endpoints });
      log('stage_start', `${STAGE_LABEL[d.stage] ?? d.stage}: ${d.total_endpoints} endpoints`);
    });
    on('endpoint_start', d => log('endpoint_start', `${d.key} — generating (${d.index}/${d.total})`));
    on('endpoint_done', d => {
      setDone(p => ({ ...p, current: p.current + 1 }));
      log('endpoint_done', `${d.key} — ${d.items} item${d.items === 1 ? '' : 's'}`);
    });
    on('endpoint_error', d => log('endpoint_error', `${d.key} — failed: ${d.error}`));
    on('review_start', d => log('review_start', `${d.key} — reviewing`));
    on('review_done', d => {
      setDone(p => ({ ...p, current: p.current + 1 }));
      log('review_done', `${d.key} — ${d.verdict}, ${d.finding_count} finding${d.finding_count === 1 ? '' : 's'}`);
    });
    on('revision_done', d =>
      log('revision_done', `${d.key} — ${d.applied} applied, ${d.raised} raised`));
    on('gate_reached', d => {
      log('gate_reached', `Gate reached: ${d.gate}${d.errored_endpoints ? ` (${d.errored_endpoints} endpoint(s) errored)` : ''}`);
      getRun(id).then(setRun).catch(() => undefined);
      navigate(d.gate === 'requirements' ? `/runs/${id}/requirements` : `/runs/${id}/cases`);
    });
    on('run_done', () => { getRun(id).then(setRun).catch(() => undefined); });
    on('run_error', d => setError(d.error));

    source.onerror = () => {
      // The stream ends when the run reaches a gate; re-read the run rather than
      // trusting replayed events, then stop retrying.
      if (!closed) getRun(id).then(setRun).catch(() => undefined);
    };

    return () => { closed = true; source.close(); };   // captures this source, not stale state
  }, [runId, navigate]);

  if (error) return (
    <div className="screen">
      <div className="alert alert-error">{error}</div>
      <button className="btn btn-ghost" onClick={() => navigate('/specs')}>Back to specs</button>
    </div>
  );
  if (!run) return <div className="screen">Loading run…</div>;

  const pct = done.total ? Math.round((done.current / done.total) * 100) : 0;
  const working = [RunStatus.GENERATING_REQUIREMENTS, RunStatus.REVIEWING_REQUIREMENTS,
                   RunStatus.GENERATING_CASES, RunStatus.REVIEWING_CASES].includes(run.status);

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Run #{run.id}</h1>
        <span className={`status-pill status-${run.status}`}>{formatStatus(run.status)}</span>
      </div>

      <p className="muted">{run.provider} · {run.model}
        {run.reviewer_model && ` · reviewed by ${run.reviewer_model}`}</p>

      {run.error && <div className="alert alert-error">{run.error}</div>}

      {working && (
        <div className="progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="progress-fill" style={{ width: `${pct}%` }} />
          <span className="progress-label">{phase} · {done.current} / {done.total || '?'}</span>
        </div>
      )}

      <div className="event-log">
        {lines.length === 0 && <p className="muted">Waiting for the first endpoint…</p>}
        {lines.map(line => (
          <div key={line.key} className={`event-line event-${line.event}`}>{line.text}</div>
        ))}
      </div>

      <div className="screen-footer">
        {run.status === RunStatus.AWAITING_REQUIREMENTS_APPROVAL && (
          <button className="btn btn-primary" onClick={() => navigate(`/runs/${run.id}/requirements`)}>
            Review requirements — gate 1
          </button>
        )}
        {run.status === RunStatus.AWAITING_CASE_APPROVAL && (
          <button className="btn btn-primary" onClick={() => navigate(`/runs/${run.id}/cases`)}>
            Review test cases — gate 2
          </button>
        )}
        {run.status === RunStatus.DONE && (
          <button className="btn btn-primary" onClick={() => navigate(`/runs/${run.id}/export`)}>
            Export
          </button>
        )}
      </div>
    </div>
  );
}
