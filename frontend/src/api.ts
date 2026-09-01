import {
  RunStatus,
  type Category, type Endpoint, type ExecutionResult, type GeneratedProject, type GenerationRun,
  type ImportSummary, type ProviderInfo, type Requirement, type Review, type ReviewStage,
  type SandboxStatus, type Spec, type TestCase, type TraceabilityReport,
} from './types';

const API = '/api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    ...options,
    headers: options?.body instanceof FormData
      ? options?.headers
      : { 'Content-Type': 'application/json', ...options?.headers },
  });
  if (!resp.ok) {
    // FastAPI puts the useful part in `detail`, which may itself be an object.
    let detail: unknown = await resp.text();
    try {
      const parsed = JSON.parse(detail as string);
      detail = parsed.detail ?? parsed;
    } catch { /* plain text */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return resp.status === 204 ? (undefined as T) : resp.json();
}

// ---- specs ----
export const listSpecs = () => request<Spec[]>(`${API}/specs`);
export const getSpec = (id: number) => request<Spec>(`${API}/specs/${id}`);
export const listEndpoints = (id: number) => request<Endpoint[]>(`${API}/specs/${id}/endpoints`);
export const deleteSpec = (id: number) => request<void>(`${API}/specs/${id}`, { method: 'DELETE' });

export function uploadSpec(file: File): Promise<Spec> {
  const form = new FormData();
  form.append('file', file);
  return request<Spec>(`${API}/specs`, { method: 'POST', body: form });
}

export const uploadSpecFromUrl = (url: string) =>
  request<Spec>(`${API}/specs/from-url`, { method: 'POST', body: JSON.stringify({ url }) });

// ---- providers ----
export const listProviders = () => request<ProviderInfo[]>(`${API}/providers`);

// ---- runs ----
export const createRun = (data: {
  spec_id: number; provider: string; model: string;
  reviewer_provider?: string; reviewer_model?: string;
  categories: Category[]; endpoint_keys: string[];
}) => request<GenerationRun>(`${API}/runs`, { method: 'POST', body: JSON.stringify(data) });

export const listRuns = (specId?: number) =>
  request<GenerationRun[]>(specId ? `${API}/runs?spec_id=${specId}` : `${API}/runs`);
export const getRun = (id: number) => request<GenerationRun>(`${API}/runs/${id}`);
export const startRun = (id: number) =>
  request<GenerationRun>(`${API}/runs/${id}/start`, { method: 'POST' });

/** Gate 1: requires every requirement approved, then starts stage 2. */
export const approveRequirementsGate = (id: number) =>
  request<GenerationRun>(`${API}/runs/${id}/approve-requirements`, { method: 'POST' });

/** Gate 2: requires every case approved, then completes the run. */
export const approveCasesGate = (id: number) =>
  request<GenerationRun>(`${API}/runs/${id}/approve-cases`, { method: 'POST' });

export const regenerate = (id: number, endpoint_key: string, stage: ReviewStage) =>
  request<{ created: unknown[]; rejected: string[] }>(`${API}/runs/${id}/regenerate`,
    { method: 'POST', body: JSON.stringify({ endpoint_key, stage }) });

// ---- requirements ----
export const listRequirements = (runId: number) =>
  request<Requirement[]>(`${API}/runs/${runId}/requirements`);
export const updateRequirement = (pk: number, data: Partial<Requirement>) =>
  request<Requirement>(`${API}/requirements/${pk}`, { method: 'PATCH', body: JSON.stringify(data) });
export const deleteRequirement = (pk: number) =>
  request<void>(`${API}/requirements/${pk}`, { method: 'DELETE' });
export const createRequirement = (data: Record<string, unknown>) =>
  request<Requirement>(`${API}/requirements`, { method: 'POST', body: JSON.stringify(data) });
export const approveRequirements = (body: { run_id?: number; all?: boolean; requirement_ids?: string[] }) =>
  request<{ approved: number }>(`${API}/requirements/approve`, { method: 'POST', body: JSON.stringify(body) });

// ---- test cases ----
export const listCases = (runId: number) => request<TestCase[]>(`${API}/runs/${runId}/cases`);
export const updateCase = (pk: number, data: Partial<TestCase>) =>
  request<TestCase>(`${API}/cases/${pk}`, { method: 'PATCH', body: JSON.stringify(data) });
export const deleteCase = (pk: number) => request<void>(`${API}/cases/${pk}`, { method: 'DELETE' });
export const createCase = (data: Record<string, unknown>) =>
  request<TestCase>(`${API}/cases`, { method: 'POST', body: JSON.stringify(data) });
export const approveCases = (body: { run_id?: number; all?: boolean; case_ids?: string[] }) =>
  request<{ approved: number }>(`${API}/cases/approve`, { method: 'POST', body: JSON.stringify(body) });
export const getTraceability = (runId: number) =>
  request<TraceabilityReport>(`${API}/runs/${runId}/traceability`);

// ---- reviews ----
export const listReviews = (runId: number, stage?: ReviewStage) =>
  request<Review[]>(stage ? `${API}/runs/${runId}/reviews?stage=${stage}` : `${API}/runs/${runId}/reviews`);

/** Undo one applied finding, or the whole artifact when findingId is omitted. */
export const revert = (runId: number, targetId: string, findingId?: string) =>
  request<Requirement | TestCase>(
    `${API}/runs/${runId}/revert/${encodeURIComponent(targetId)}` +
    (findingId ? `?finding_id=${encodeURIComponent(findingId)}` : ''), { method: 'POST' });

// ---- export / import ----
export function exportUrl(runId: number, format: 'json' | 'xlsx', onlyApproved: boolean): string {
  return `${API}/runs/${runId}/export?format=${format}&only_approved=${onlyApproved}`;
}

export function importFile(runId: number, file: File): Promise<ImportSummary> {
  const form = new FormData();
  form.append('file', file);
  return request<ImportSummary>(`${API}/runs/${runId}/import`, { method: 'POST', body: form });
}

// ---- display helpers ----
export const formatStatus = (status: string) =>
  status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());

/** Where to send someone who clicks on a run, based on what it's waiting on. */
export function runPath(run: GenerationRun): string {
  switch (run.status) {
    case RunStatus.AWAITING_REQUIREMENTS_APPROVAL: return `/runs/${run.id}/requirements`;
    case RunStatus.AWAITING_CASE_APPROVAL: return `/runs/${run.id}/cases`;
    case RunStatus.DONE: return `/runs/${run.id}/export`;
    default: return `/runs/${run.id}/progress`;
  }
}

export const preview = (value: unknown): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
};

// ---- phase 2: codegen, execution, reports ----
export const sandboxStatus = () => request<SandboxStatus>(`${API}/sandbox`);

export const generateProject = (runId: number, body: {
  base_url: string; test_data: Record<string, string>; only_approved: boolean;
}) => request<GeneratedProject>(`${API}/runs/${runId}/codegen`,
  { method: 'POST', body: JSON.stringify(body) });

export const projectDownloadUrl = (runId: number) => `${API}/runs/${runId}/codegen/download`;

export const executeProject = (runId: number) =>
  request<ExecutionResult>(`${API}/runs/${runId}/execute`,
    { method: 'POST', body: JSON.stringify({}) });

export const getExecution = (runId: number) =>
  request<ExecutionResult>(`${API}/runs/${runId}/execution`);

export const getProjectFile = (runId: number, path: string) =>
  request<{ path: string; content: string }>(`${API}/runs/${runId}/codegen/files/${path}`);

export const updateProjectFile = (runId: number, path: string, content: string) =>
  request<{ path: string; saved: boolean }>(`${API}/runs/${runId}/codegen/files/${path}`,
    { method: 'PUT', body: JSON.stringify({ content }) });
