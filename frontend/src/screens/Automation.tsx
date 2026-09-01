import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { CaseResult, ExecutionResult, GeneratedProject, GenerationRun, SandboxStatus } from '../types';
import {
  executeProject, generateProject, getExecution, getProjectFile, getRun, projectDownloadUrl,
  sandboxStatus, updateProjectFile,
} from '../api';

type ResultFilter = 'all' | CaseResult['status'];
type Tab = 'build' | 'results';

export function Automation() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const id = Number(runId);

  const [run, setRun] = useState<GenerationRun | null>(null);
  const [sandbox, setSandbox] = useState<SandboxStatus | null>(null);
  const [project, setProject] = useState<GeneratedProject | null>(null);
  const [result, setResult] = useState<ExecutionResult | null>(null);
  const [baseUrl, setBaseUrl] = useState('http://host.docker.internal:8080');
  const [testData, setTestData] = useState([{ key: 'authHeader', value: '' }, { key: 'authValue', value: '' }]);
  const [busy, setBusy] = useState<'' | 'generating' | 'running'>('');
  const [error, setError] = useState<string | null>(null);
  const [resultFilter, setResultFilter] = useState<ResultFilter>('all');
  const [tab, setTab] = useState<Tab>('build');

  const [openFile, setOpenFile] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState('');
  const [fileBusy, setFileBusy] = useState(false);
  const [fileSaved, setFileSaved] = useState(false);

  useEffect(() => {
    getRun(id).then(setRun).catch(e => setError(String(e)));
    sandboxStatus().then(setSandbox).catch(() => undefined);
    getExecution(id).then(r => { setResult(r); setTab('results'); }).catch(() => undefined);   // 404 until first run
  }, [id]);

  function setTestDataField(i: number, field: 'key' | 'value', v: string) {
    setTestData(prev => prev.map((row, idx) => (idx === i ? { ...row, [field]: v } : row)));
  }
  function addTestDataRow(key = '') {
    setTestData(prev => [...prev, { key, value: '' }]);
  }
  function removeTestDataRow(i: number) {
    setTestData(prev => prev.filter((_, idx) => idx !== i));
  }

  async function generate() {
    setBusy('generating');
    setError(null);
    try {
      const data: Record<string, string> = {};
      for (const { key, value } of testData) if (key && value) data[key] = value;
      setProject(await generateProject(id, { base_url: baseUrl, test_data: data, only_approved: true }));
      setOpenFile(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy('');
    }
  }

  async function execute() {
    setBusy('running');
    setError(null);
    try {
      setResult(await executeProject(id));
      setTab('results');
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy('');
    }
  }

  async function openFileFor(path: string) {
    setOpenFile(path);
    setFileSaved(false);
    try {
      const { content } = await getProjectFile(id, path);
      setFileContent(content);
    } catch (e) {
      setError(String(e));
      setOpenFile(null);
    }
  }

  async function saveFile() {
    if (!openFile) return;
    setFileBusy(true);
    setError(null);
    try {
      await updateProjectFile(id, openFile, fileContent);
      setFileSaved(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setFileBusy(false);
    }
  }

  if (!run) return <div className="screen">{error ?? 'Loading…'}</div>;

  const usedKeys = new Set(testData.map(r => r.key));
  const placeholders = (project?.placeholders ?? [])
    .filter(p => !['baseUrl'].includes(p) && !usedKeys.has(p));

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>Run #{run.id} · Automation</h1>
        <button className="btn btn-ghost" onClick={() => navigate(`/runs/${id}/export`)}>Export</button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <div className="tab-row">
        <button className={`tab ${tab === 'build' ? 'tab-on' : ''}`} onClick={() => setTab('build')}>
          Build
        </button>
        <button className={`tab ${tab === 'results' ? 'tab-on' : ''}`} onClick={() => setTab('results')}>
          Results{result ? ` (${result.passed}/${result.total})` : ''}
        </button>
      </div>

      {tab === 'build' && <>
      <section className="config-section">
        <h2>1 · Generate the project</h2>
        <p className="muted">
          Approved test cases become a Maven project using TestNG and REST Assured.
          Credentials live in <code>testronaut.properties</code>, never in the generated source.
          Review any generated file below and edit it in place before you run it.
        </p>
        <div className="field-row">
          <label>Base URL of the system under test
            <input type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} /></label>
        </div>
        <p className="muted">
          From a container, the host is <code>host.docker.internal</code>, not <code>localhost</code>.
        </p>

        <div className="kv-list">
          {testData.map((row, i) => (
            <div className="kv-row" key={i}>
              <input placeholder="name" value={row.key}
                     onChange={e => setTestDataField(i, 'key', e.target.value)} />
              <input placeholder="value" value={row.value}
                     onChange={e => setTestDataField(i, 'value', e.target.value)} />
              <button className="btn btn-sm btn-ghost" onClick={() => removeTestDataRow(i)}>Remove</button>
            </div>
          ))}
          <button className="btn btn-sm btn-secondary" onClick={() => addTestDataRow()}>+ Add test data</button>
        </div>

        <div className="row-actions">
          <button className={`btn ${project ? 'btn-ghost' : 'btn-primary'}`}
                  disabled={busy !== ''} onClick={() => void generate()}>
            {busy === 'generating' ? 'Generating…' : project ? 'Regenerate' : 'Generate project'}
          </button>
        </div>

        {project && (
          <div className="artifact-card">
            <div className="artifact-header">
              <div>
                <span className="artifact-title">testronaut-run-{run.id}.zip</span>
                <span className="muted">
                  {' '}· {project.files.length} file{project.files.length === 1 ? '' : 's'} ·{' '}
                  <strong>{project.test_count}</strong> test methods across{' '}
                  <strong>{project.endpoint_count}</strong> endpoints ·{' '}
                  <strong>{project.compiled_assertions}</strong> assertions compiled
                </span>
              </div>
              <a className="btn btn-primary" href={projectDownloadUrl(id)}>Download .zip</a>
            </div>
            <ul className="file-manifest">
              {project.files.slice().sort().map(f => (
                <li key={f}>
                  <button className={`file-row ${openFile === f ? 'file-row-open' : ''}`}
                          onClick={() => void openFileFor(f)}>
                    <code>{f}</code>
                  </button>
                </li>
              ))}
            </ul>

            {openFile && (
              <div className="file-editor">
                <div className="file-editor-head">
                  <code>{openFile}</code>
                  <div className="row-actions">
                    {fileSaved && <span className="muted">Saved — will be used on the next run.</span>}
                    <button className="btn btn-sm btn-primary" disabled={fileBusy}
                            onClick={() => void saveFile()}>
                      {fileBusy ? 'Saving…' : 'Save'}
                    </button>
                    <button className="btn btn-sm btn-ghost" onClick={() => setOpenFile(null)}>Close</button>
                  </div>
                </div>
                <textarea className="file-editor-body" spellCheck={false} value={fileContent}
                          onChange={e => { setFileContent(e.target.value); setFileSaved(false); }} />
              </div>
            )}

            {project.skipped_assertions.length > 0 && (
              <details>
                <summary>
                  {project.skipped_assertions.length} assertion(s) could not be compiled —
                  left as TODO comments
                </summary>
                <ul className="criteria">
                  {project.skipped_assertions.map((a, i) => <li key={i}><code>{a}</code></li>)}
                </ul>
                <p className="muted">
                  Rewrite these in the assertion grammar (<code>$.path == value</code>) on the
                  test case, then regenerate.
                </p>
              </details>
            )}
            {placeholders.length > 0 && (
              <p className="muted">
                Test data still needed:{' '}
                {placeholders.map(p => (
                  <button key={p} className="btn btn-sm btn-ghost" onClick={() => addTestDataRow(p)}>
                    + <code>{p}</code>
                  </button>
                ))}
              </p>
            )}
          </div>
        )}
      </section>

      <section className="config-section">
        <h2>2 · Run it</h2>
        {sandbox && !sandbox.available && (
          <div className="alert alert-warn">{sandbox.reason}</div>
        )}
        <p className="muted">
          The generated code came from a model that read an untrusted spec, so it runs in a
          container — never on this machine.
        </p>
        <button className="btn btn-primary"
                disabled={busy !== '' || !project || !(sandbox?.available ?? false)}
                onClick={() => void execute()}>
          {busy === 'running' ? 'Running in sandbox…' : 'Run tests'}
        </button>
      </section>
      </>}

      {tab === 'results' && (
        <section className="config-section">
          {!result ? (
            <p className="muted">No results yet — run the tests from the Build tab to see them here.</p>
          ) : <>
          <div className="summary-row">
            <div className="summary-item"><span className="summary-number">{result.total}</span>
              <span className="summary-label">Tests</span></div>
            <div className="summary-item"><span className="summary-number result-passed">{result.passed}</span>
              <span className="summary-label">Passed</span></div>
            <div className="summary-item"><span className="summary-number result-failed">{result.failed}</span>
              <span className="summary-label">Failed</span></div>
            <div className="summary-item"><span className="summary-number result-error">{result.errors}</span>
              <span className="summary-label">Errors</span></div>
            <div className="summary-item"><span className="summary-number">{result.duration}s</span>
              <span className="summary-label">Duration</span></div>
          </div>

          {result.total > 0 && (
            <div className="result-bar">
              {result.passed > 0 &&
                <div className="result-bar-seg result-bar-passed" style={{ width: `${100 * result.passed / result.total}%` }} />}
              {result.failed > 0 &&
                <div className="result-bar-seg result-bar-failed" style={{ width: `${100 * result.failed / result.total}%` }} />}
              {result.errors > 0 &&
                <div className="result-bar-seg result-bar-error" style={{ width: `${100 * result.errors / result.total}%` }} />}
              {result.skipped > 0 &&
                <div className="result-bar-seg result-bar-skipped" style={{ width: `${100 * result.skipped / result.total}%` }} />}
            </div>
          )}

          {result.timed_out && <div className="alert alert-error">The run timed out.</div>}

          <div className="filter-row">
            {(['all', 'passed', 'failed', 'error', 'skipped'] as const).map(status => {
              const n = status === 'all' ? result.cases.length
                : result.cases.filter(c => c.status === status).length;
              return n === 0 ? null : (
                <button key={status} className={`chip ${resultFilter === status ? 'chip-on' : ''}`}
                        onClick={() => setResultFilter(status)}>
                  {status === 'all' ? 'All' : status[0].toUpperCase() + status.slice(1)} ({n})
                </button>
              );
            })}
          </div>

          <table className="artifact-table">
            <thead><tr><th>Case</th><th>Result</th><th>Time</th><th>Detail</th></tr></thead>
            <tbody>
              {result.cases
                .filter(c => resultFilter === 'all' || c.status === resultFilter)
                .map(c => (
                  <tr key={`${c.class_name}#${c.method}`}>
                    <td><code>{c.case_id ?? `${c.class_name}#${c.method}`}</code></td>
                    <td><span className={`badge badge-result-${c.status}`}>{c.status}</span></td>
                    <td>{c.time.toFixed(2)}s</td>
                    <td className="result-message">{c.message ?? '—'}</td>
                  </tr>
                ))}
            </tbody>
          </table>

          <details>
            <summary>Maven output</summary>
            <pre className="build-output">{result.output}</pre>
          </details>
          </>}
        </section>
      )}
    </div>
  );
}
