// Mirrors backend/db.py. JSON columns arrive as real arrays/objects, not strings.

export enum RunStatus {
  CREATED = 'created',
  GENERATING_REQUIREMENTS = 'generating_requirements',
  REVIEWING_REQUIREMENTS = 'reviewing_requirements',
  AWAITING_REQUIREMENTS_APPROVAL = 'awaiting_requirements_approval',
  GENERATING_CASES = 'generating_cases',
  REVIEWING_CASES = 'reviewing_cases',
  AWAITING_CASE_APPROVAL = 'awaiting_case_approval',
  DONE = 'done',
  ERROR = 'error',
}

export enum ArtifactStatus {
  DRAFT = 'draft',
  REVISED = 'revised',
  EDITED = 'edited',
  APPROVED = 'approved',
}

export enum Category {
  HAPPY_PATH = 'happy_path',
  NEGATIVE = 'negative',
  BOUNDARY = 'boundary',
  AUTH = 'auth',
  SECURITY = 'security',
}

export enum ReviewStage {
  REQUIREMENTS = 'requirements',
  TEST_CASES = 'test_cases',
}

export type Priority = 'P1' | 'P2' | 'P3';
export type Severity = 'high' | 'medium' | 'low';
export type Resolution = 'applied' | 'raised' | 'reverted';

export const CATEGORY_LABELS: Record<Category, string> = {
  [Category.HAPPY_PATH]: 'Happy path',
  [Category.NEGATIVE]: 'Negative',
  [Category.BOUNDARY]: 'Boundary',
  [Category.AUTH]: 'Auth',
  [Category.SECURITY]: 'Security',
};

export interface Spec {
  id: number;
  name: string;
  version: string;
  source: string;
  source_ref: string;
  format: string;
  endpoint_count: number;
  created_at: string;
}

export interface GenerationRun {
  id: number;
  spec_id: number;
  provider: string;
  model: string;
  reviewer_provider?: string | null;
  reviewer_model?: string | null;
  categories: string[];
  endpoint_keys: string[];
  status: RunStatus;
  error?: string | null;
  created_at: string;
  finished_at?: string | null;
}

export interface Requirement {
  id: number;
  req_id: string;
  run_id: number;
  method: string;
  path: string;
  operation_id?: string | null;
  endpoint_key: string;
  title: string;
  description: string;
  acceptance_criteria: string[];
  spec_source?: string | null;
  priority: Priority;
  origin: string;
  status: ArtifactStatus;
  snapshot?: Record<string, unknown> | null;
  order_index: number;
}

export interface TestCase {
  id: number;
  case_id: string;
  run_id: number;
  requirement_ids: string[];
  method: string;
  path: string;
  operation_id?: string | null;
  endpoint_key: string;
  category: Category;
  title: string;
  description: string;
  priority: Priority;
  preconditions: string[];
  path_params: Record<string, unknown>;
  query_params: Record<string, unknown>;
  headers: Record<string, unknown>;
  body?: unknown;
  expected_status: number;
  expected_body: string[];
  test_data_notes: string;
  status: ArtifactStatus;
  origin: string;
  snapshot?: Record<string, unknown> | null;
  order_index: number;
}

/** One thing the critic objected to, and what it did about it. */
export interface Finding {
  finding_id: string;
  severity: Severity;
  target_id: string;
  field: string;
  issue: string;
  suggestion?: unknown;
  resolution: Resolution;
  before?: unknown;
  after?: unknown;
}

export interface Review {
  id: number;
  run_id: number;
  stage: ReviewStage;
  endpoint_key: string;
  verdict: 'pass' | 'revised';
  provider: string;
  model: string;
  findings: Finding[];
  created_at: string;
}

export interface ProviderInfo {
  id: string;
  label: string;
  available: boolean;
  reason?: string | null;
  models: string[];
}

export interface Endpoint {
  key: string;
  method: string;
  path: string;
  operation_id?: string | null;
  summary?: string | null;
  parameters: unknown[];
  request_body_schema?: unknown;
  responses: Record<string, unknown>;
  security: unknown[];
}

export interface TraceabilityReport {
  covered: number;
  total_requirements: number;
  uncovered_requirement_ids: string[];
  orphan_case_ids: string[];
  duplicate_case_ids: string[];
  orphan_details: { case_id: string; reason: string }[];
  endpoints_without_cases: string[];
  cases_total: number;
  requirements_total: number;
  fully_traced: boolean;
}

export interface ArtifactImportSummary {
  inserted: number;
  updated: number;
  unchanged: number;
  missing_from_file: string[];
  errors: { sheet: string; row: number; message: string }[];
}

export interface ImportSummary {
  requirements: ArtifactImportSummary;
  cases: ArtifactImportSummary;
}

// ---- Phase 2: codegen, execution, reports ----

export interface GeneratedProject {
  root: string;
  files: string[];
  test_count: number;
  endpoint_count: number;
  compiled_assertions: number;
  skipped_assertions: string[];
  placeholders: string[];
}

export interface CaseResult {
  case_id: string | null;
  class_name: string;
  method: string;
  status: 'passed' | 'failed' | 'error' | 'skipped';
  time: number;
  message?: string | null;
  detail?: string | null;
}

export interface ExecutionResult {
  exit_code: number;
  duration: number;
  total: number;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  timed_out: boolean;
  cases: CaseResult[];
  output: string;
}

export interface SandboxStatus {
  available: boolean;
  reason: string | null;
}
