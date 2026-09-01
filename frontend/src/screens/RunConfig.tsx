import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Category, CATEGORY_LABELS, type Endpoint, type ProviderInfo, type Spec } from '../types';
import { createRun, getSpec, listEndpoints, listProviders, startRun } from '../api';

const ALL_CATEGORIES = Object.values(Category);

export function RunConfig() {
  const { specId } = useParams<{ specId: string }>();
  const navigate = useNavigate();

  const [spec, setSpec] = useState<Spec | null>(null);
  const [endpoints, setEndpoints] = useState<Endpoint[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [reviewerProvider, setReviewerProvider] = useState('');   // '' = same as generator
  const [reviewerModel, setReviewerModel] = useState('');
  const [categories, setCategories] = useState<Category[]>([Category.HAPPY_PATH, Category.NEGATIVE]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (!specId) return;
    const id = Number(specId);
    Promise.all([getSpec(id), listEndpoints(id), listProviders()])
      .then(([s, e, p]) => {
        setSpec(s);
        setEndpoints(e);
        setSelected(new Set(e.map(x => x.key)));
        setProviders(p);
        const first = p.find(x => x.available) ?? p[0];
        if (first) {
          setProvider(first.id);
          setModel(first.models[0] ?? '');
        }
      })
      .catch(e => setError(String(e)));
  }, [specId]);

  const current = providers.find(p => p.id === provider);
  // The critic may live on another provider entirely — generating free on a local
  // model and reviewing with a strong hosted one is the whole point of the option.
  const reviewer = providers.find(p => p.id === (reviewerProvider || provider));

  function toggle<T>(set: Set<T>, value: T): Set<T> {
    const next = new Set(set);
    next.has(value) ? next.delete(value) : next.add(value);
    return next;
  }

  async function launch() {
    if (!spec) return;
    setStarting(true);
    setError(null);
    try {
      const run = await createRun({
        spec_id: spec.id, provider, model,
        reviewer_provider: reviewerProvider || undefined,
        reviewer_model: reviewerModel || undefined,
        categories, endpoint_keys: [...selected],
      });
      await startRun(run.id);                       // create and start are separate calls
      navigate(`/runs/${run.id}/progress`);
    } catch (e) {
      setError(String(e));
      setStarting(false);
    }
  }

  if (error && !spec) return <div className="screen"><div className="alert alert-error">{error}</div></div>;
  if (!spec) return <div className="screen">Loading…</div>;

  const ready = provider && model && categories.length > 0 && selected.size > 0;

  return (
    <div className="screen">
      <div className="screen-header">
        <h1>New run · {spec.name}</h1>
        <button className="btn btn-ghost" onClick={() => navigate('/specs')}>Back</button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <section className="config-section">
        <h2>Model</h2>
        <div className="field-row">
          <label>
            Provider
            <select value={provider} onChange={e => {
              setProvider(e.target.value);
              setModel(providers.find(p => p.id === e.target.value)?.models[0] ?? '');
            }}>
              {providers.map(p => (
                <option key={p.id} value={p.id} disabled={!p.available}>
                  {p.label}{p.available ? '' : ` — ${p.reason ?? 'unavailable'}`}
                </option>
              ))}
            </select>
          </label>

          <label>
            Model
            <select value={model} onChange={e => setModel(e.target.value)}>
              {(current?.models ?? []).map(m => <option key={m} value={m}>{m}</option>)}
              {(current?.models ?? []).length === 0 && <option value="">no models found</option>}
            </select>
          </label>

          <label>
            Reviewer provider <span className="muted">(optional)</span>
            <select value={reviewerProvider} onChange={e => {
              setReviewerProvider(e.target.value);
              setReviewerModel('');
            }}>
              <option value="">same as generator</option>
              {providers.map(p => (
                <option key={p.id} value={p.id} disabled={!p.available}>
                  {p.label}{p.available ? '' : ` — ${p.reason ?? 'unavailable'}`}
                </option>
              ))}
            </select>
          </label>

          <label>
            Reviewer model <span className="muted">(optional)</span>
            <select value={reviewerModel} onChange={e => setReviewerModel(e.target.value)}>
              <option value="">
                {reviewerProvider ? `default for ${reviewer?.label}` : 'same as generator'}
              </option>
              {(reviewer?.models ?? []).map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
        </div>
        {current && !current.available && (
          <p className="alert alert-warn">{current.label} is not available: {current.reason}</p>
        )}
      </section>

      <section className="config-section">
        <h2>Coverage categories</h2>
        <div className="checkbox-row">
          {ALL_CATEGORIES.map(cat => (
            <label key={cat} className="checkbox">
              <input type="checkbox" checked={categories.includes(cat)}
                     onChange={() => setCategories([...toggle(new Set(categories), cat)])} />
              {CATEGORY_LABELS[cat]}
            </label>
          ))}
        </div>
      </section>

      <section className="config-section">
        <div className="section-head">
          <h2>Endpoints <span className="muted">({selected.size} of {endpoints.length})</span></h2>
          <div>
            <button className="btn btn-sm btn-ghost"
                    onClick={() => setSelected(new Set(endpoints.map(e => e.key)))}>All</button>
            <button className="btn btn-sm btn-ghost" onClick={() => setSelected(new Set())}>None</button>
          </div>
        </div>
        <div className="endpoint-list">
          {endpoints.map(ep => (
            <label key={ep.key} className="endpoint-row">
              <input type="checkbox" checked={selected.has(ep.key)}
                     onChange={() => setSelected(toggle(selected, ep.key))} />
              <span className={`method method-${ep.method.toLowerCase()}`}>{ep.method}</span>
              <code>{ep.path}</code>
              <span className="muted truncate">{ep.summary}</span>
            </label>
          ))}
        </div>
      </section>

      <div className="screen-footer">
        <p className="muted">
          Generates requirements for {selected.size} endpoint{selected.size === 1 ? '' : 's'},
          then stops for your approval before any test case is written.
        </p>
        <button className="btn btn-primary" disabled={!ready || starting} onClick={() => void launch()}>
          {starting ? 'Starting…' : 'Generate requirements'}
        </button>
      </div>
    </div>
  );
}
