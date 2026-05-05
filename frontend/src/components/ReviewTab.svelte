<script lang="ts">
  import {
    cachedReview,
    stagedReviewFindings,
    publishReviewsModalOpen,
  } from '../stores/prs';
  import type { Repo, PR } from '../lib/types';
  import StreamingReview from './StreamingReview.svelte';
  import FindingCard from './FindingCard.svelte';
  import PublishReviewsModal from './PublishReviewsModal.svelte';

  let { repo, pr }: { repo: Repo; pr: PR } = $props();

  type ViewMode = 'idle' | 'streaming' | 'results';

  let mode = $state<ViewMode>('idle');
  let rerun = $state(false);

  // Transition idle → results when a cached review is already present on mount.
  $effect(() => {
    if ($cachedReview && mode === 'idle') {
      mode = 'results';
    }
  });

  function startReview(isRerun = false) {
    rerun = isRerun;
    mode = 'streaming';
    cachedReview.set(null);
  }

  function handleReviewComplete() {
    mode = 'results';
  }
</script>

<div class="review-tab">
  <div class="actions">
    {#if mode === 'idle'}
      <button class="btn btn-accent" onclick={() => startReview(false)}>▶ Run Review</button>
    {:else if mode === 'results'}
      <button class="btn btn-sm" onclick={() => startReview(true)}>↻ Re-run</button>
      <span class="cache-note">Showing cached review</span>
    {:else if mode === 'streaming'}
      <span style="color:var(--text-muted);font-size:12px">Review in progress…</span>
    {/if}
  </div>

  {#if mode === 'streaming'}
    <StreamingReview {repo} {pr} {rerun} oncomplete={handleReviewComplete} />
  {:else if mode === 'results' && $cachedReview}
    <div class="findings-list">
      {#each $cachedReview.findings as finding, i (i)}
        <FindingCard {finding} {repo} {pr} index={i} />
      {/each}
    </div>

    <div class="batch-publish">
      <button
        class="btn btn-warn"
        onclick={() => publishReviewsModalOpen.set(true)}
        disabled={$stagedReviewFindings.length === 0}
        title="Publish all staged findings as a single Request Changes review"
      >
        ⚠ Publish reviews
        {#if $stagedReviewFindings.length > 0}
          <span class="badge-count">{$stagedReviewFindings.length}</span>
        {/if}
      </button>
      {#if $stagedReviewFindings.length === 0}
        <span class="hint">Click <strong>+ Add to review</strong> on findings to stage them.</span>
      {/if}
    </div>
  {:else if mode === 'idle'}
    <div class="idle-state">No review yet. Run the review to analyze this PR.</div>
  {/if}
</div>

<PublishReviewsModal {repo} {pr} />

<style>
  .review-tab { display: flex; flex-direction: column; gap: 14px; }
  .actions { display: flex; align-items: center; gap: 10px; }
  .cache-note { font-size: 10px; color: var(--text-muted); }
  .findings-list { display: flex; flex-direction: column; gap: 0; }
  .idle-state { padding: 40px 0; text-align: center; color: var(--text-muted); font-size: 12px; }
  .batch-publish {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--glass-border);
  }
  .batch-publish .hint {
    font-size: 11px;
    color: var(--text-muted);
  }
  .btn-warn {
    background: rgba(255, 140, 66, 0.16);
    border: 1px solid rgba(255, 140, 66, 0.45);
    color: rgb(255, 175, 110);
  }
  .btn-warn:hover:not(:disabled) {
    background: rgba(255, 140, 66, 0.25);
  }
  .btn-warn:disabled { opacity: 0.4; cursor: not-allowed; }
  .badge-count {
    display: inline-block;
    margin-left: 6px;
    background: rgba(255, 140, 66, 0.3);
    color: rgb(255, 200, 150);
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 8px;
    font-weight: 700;
  }
</style>
