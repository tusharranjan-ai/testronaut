import type { Finding, Review } from '../types';
import { preview } from '../api';

/**
 * What the critic objected to, what it changed, and a way to undo it.
 *
 * An agent that silently rewrites its own output is a black box; the point of
 * the review stage is that every edit is inspectable and individually revertable.
 */
export function FindingsPanel({ reviews, onRevert, filterTarget }: {
  reviews: Review[];
  onRevert: (targetId: string, findingId: string) => void;
  filterTarget?: string;
}) {
  const findings: { finding: Finding; endpoint: string }[] = reviews.flatMap(review =>
    review.findings
      .filter(f => !filterTarget || f.target_id === filterTarget)
      .map(finding => ({ finding, endpoint: review.endpoint_key })),
  );

  if (reviews.length === 0) {
    return <div className="findings-panel"><h3>Findings</h3>
      <p className="muted">The review has not run yet.</p></div>;
  }

  const applied = findings.filter(f => f.finding.resolution === 'applied').length;
  const reverted = findings.filter(f => f.finding.resolution === 'reverted').length;

  return (
    <div className="findings-panel">
      <h3>Findings</h3>
      <p className="muted">
        {findings.length} total · {applied} applied · {findings.length - applied - reverted} raised
        {reverted > 0 && ` · ${reverted} reverted`}
      </p>

      {findings.length === 0 ? (
        <p className="muted">The critic found nothing to object to.</p>
      ) : (
        <ul className="findings-list">
          {findings.map(({ finding, endpoint }) => (
            <li key={`${endpoint}-${finding.target_id}-${finding.finding_id}`}
                className={`finding finding-${finding.resolution}`}>
              <div className="finding-head">
                <span className={`badge badge-${finding.severity}`}>{finding.severity}</span>
                <code>{finding.target_id}</code>
                {finding.field && <span className="muted">·&nbsp;{finding.field}</span>}
                <span className={`resolution resolution-${finding.resolution}`}>
                  {finding.resolution}
                </span>
              </div>

              <p className="finding-issue">{finding.issue}</p>

              {finding.resolution === 'applied' && (
                <>
                  <div className="diff">
                    <div className="diff-before"><span className="diff-label">before</span>
                      <code>{preview(finding.before)}</code></div>
                    <div className="diff-after"><span className="diff-label">after</span>
                      <code>{preview(finding.after)}</code></div>
                  </div>
                  <button className="btn btn-sm btn-secondary"
                          onClick={() => onRevert(finding.target_id, finding.finding_id)}>
                    revert
                  </button>
                </>
              )}

              {finding.resolution === 'raised' && finding.suggestion != null && (
                <p className="finding-suggestion">
                  <span className="diff-label">suggested</span> <code>{preview(finding.suggestion)}</code>
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
