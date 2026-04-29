<script lang="ts">
  import { onMount } from 'svelte';
  import { activeRepo } from '../stores/repos';
  import { activePR, activeTab } from '../stores/prs';
  import {
    aiProvider,
    providersStatus,
    providerPickerOpen,
    selectedModel,
    modelStorageKey,
    showToast,
    type ProvidersStatus,
  } from '../stores/ui';

  const PROVIDER_PICKED_KEY = 'gh_review_provider_picked';

  let availableModels = $state<string[]>([]);
  let currentProvider = $state<string>('');

  async function loadProviders(): Promise<ProvidersStatus | null> {
    try {
      const res = await fetch('/api/providers');
      if (!res.ok) return null;
      const status = (await res.json()) as ProvidersStatus;
      providersStatus.set(status);
      currentProvider = status.active;
      aiProvider.set(currentProvider);
      return status;
    } catch {
      return null;
    }
  }

  async function loadModels(): Promise<void> {
    try {
      const res = await fetch('/api/models');
      if (!res.ok) {
        availableModels = [];
        return;
      }
      const data = await res.json();
      availableModels = data.models ?? [];
      if (data.warning) showToast(data.warning, 'error');
    } catch {
      availableModels = [];
    }
  }

  function applyStoredModel(): void {
    if (!currentProvider) {
      selectedModel.set('');
      return;
    }
    // Pick the active model from provider-scoped storage so a value cached
    // for opencode (e.g. "anthropic/claude-sonnet-4-6") never leaks into a
    // claude-code session and vice-versa. We never auto-pick
    // availableModels[0]: opencode's `models` listing can include identifiers
    // its runtime later rejects. "" sends no --model flag, so each provider
    // uses its own default.
    const stored = localStorage.getItem(modelStorageKey(currentProvider)) ?? '';
    const valid = stored !== '' && availableModels.includes(stored);
    selectedModel.set(valid ? stored : '');
  }

  onMount(async () => {
    const status = await loadProviders();
    await loadModels();
    applyStoredModel();

    if (!status) return;
    // Show the picker on first load when nothing is pre-decided: no env var,
    // no previous explicit choice, AND the active provider's CLI is missing.
    // (If the CLI is present, the user can keep using the auto-picked default
    // without being interrupted.)
    const hasPicked = localStorage.getItem(PROVIDER_PICKED_KEY) === '1';
    const activeAvailable = status.available[status.active] ?? false;
    if (!status.from_env && !hasPicked && !activeAvailable) {
      providerPickerOpen.set(true);
    }
    if (status.from_env && !activeAvailable) {
      showToast(
        `AI_PROVIDER=${status.active} but ${status.clis[status.active] ?? status.active} is not on PATH.`,
        'error',
      );
    } else if (status.active) {
      showToast(`AI provider: ${status.active}`, 'info');
    }
  });

  // When the provider changes (modal pick), refresh the model list and
  // re-apply provider-scoped model storage.
  $effect(() => {
    const next = $aiProvider;
    if (!next || next === currentProvider) return;
    currentProvider = next;
    localStorage.setItem(PROVIDER_PICKED_KEY, '1');
    void (async () => {
      await loadModels();
      applyStoredModel();
    })();
  });

  function onModelChange(value: string): void {
    selectedModel.set(value);
    if (currentProvider) {
      localStorage.setItem(modelStorageKey(currentProvider), value);
    }
  }

  function goHome(): void {
    activeRepo.set(null);
    activePR.set(null);
  }

  function goRepo(): void {
    activePR.set(null);
  }

  function openPicker(): void {
    providerPickerOpen.set(true);
  }

  let activeAvailable = $derived(
    !!$providersStatus && ($providersStatus.available[$providersStatus.active] ?? false),
  );
  let providerLabel = $derived($aiProvider || 'no provider');
</script>

<header class="topbar">
  <button class="logo" onclick={goHome}>GH-REVIEW</button>
  <nav class="breadcrumb">
    {#if $activeRepo}
      <span class="sep">/</span>
      <button class="crumb" onclick={goRepo}>{$activeRepo.full_name}</button>
    {/if}
    {#if $activePR}
      <span class="sep">/</span>
      <button class="crumb active" onclick={() => activeTab.set('review')}>#{$activePR.number}</button>
    {/if}
  </nav>

  <div class="spacer"></div>

  <button
    type="button"
    class="provider-badge"
    class:provider-claude={$aiProvider === 'claude-code'}
    class:provider-missing={!activeAvailable && !!$aiProvider}
    onclick={openPicker}
    title={
      !$aiProvider
        ? 'Click to select an AI provider'
        : activeAvailable
        ? `Active AI provider: ${$aiProvider} (click to change)`
        : `${$aiProvider} CLI is not installed (click to switch)`
    }
  >
    {providerLabel}
    {#if !activeAvailable && $aiProvider}<span class="dot-warn" aria-hidden="true">!</span>{/if}
  </button>

  <div class="model-selector">
    <span class="model-label">Model</span>
    <select
      class="model-select"
      value={$selectedModel}
      onchange={(e) => onModelChange((e.target as HTMLSelectElement).value)}
      disabled={!$aiProvider}
    >
      <option value="">{$aiProvider ? `${$aiProvider} default` : 'no provider'}</option>
      {#each availableModels as m}
        <option value={m}>{m}</option>
      {/each}
    </select>
  </div>
</header>

<style>
  .topbar {
    grid-area: topbar;
    height: 52px;
    display: flex;
    align-items: center;
    gap: 0;
    padding: 0 20px;
    background: rgba(10, 10, 15, 0.85);
    backdrop-filter: var(--glass-blur);
    border-bottom: 1px solid var(--glass-border);
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .logo {
    background: none;
    border: none;
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.12em;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: var(--radius-sm);
    transition: background var(--transition-fast), box-shadow var(--transition-fast);
  }
  .logo:hover { background: rgba(255,107,53,0.08); box-shadow: var(--glow-accent); }
  .breadcrumb { display: flex; align-items: center; gap: 2px; }
  .sep { color: var(--text-muted); margin: 0 2px; font-size: 12px; }
  .crumb {
    background: none;
    border: none;
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: 12px;
    cursor: pointer;
    padding: 3px 6px;
    border-radius: var(--radius-sm);
    transition: color var(--transition-fast), background var(--transition-fast);
  }
  .crumb:hover { color: var(--text-primary); background: var(--glass-bg-hover); }
  .crumb.active { color: var(--text-primary); }

  .spacer { flex: 1; }

  .provider-badge {
    margin-right: 12px;
    padding: 3px 8px;
    border-radius: var(--radius-sm);
    background: rgba(255, 107, 53, 0.12);
    border: 1px solid rgba(255, 107, 53, 0.35);
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: background var(--transition-fast), border-color var(--transition-fast);
  }
  .provider-badge:hover {
    background: rgba(255, 107, 53, 0.2);
    border-color: rgba(255, 107, 53, 0.6);
  }
  .provider-badge.provider-claude {
    background: rgba(120, 80, 220, 0.14);
    border-color: rgba(150, 110, 240, 0.45);
    color: rgb(190, 160, 250);
  }
  .provider-badge.provider-claude:hover {
    background: rgba(120, 80, 220, 0.22);
    border-color: rgba(150, 110, 240, 0.7);
  }
  .provider-badge.provider-missing {
    background: rgba(220, 80, 80, 0.14);
    border-color: rgba(220, 80, 80, 0.45);
    color: rgb(240, 130, 130);
  }
  .provider-badge.provider-missing:hover {
    background: rgba(220, 80, 80, 0.22);
    border-color: rgba(220, 80, 80, 0.7);
  }
  .dot-warn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: rgba(220, 80, 80, 0.45);
    color: white;
    font-size: 10px;
    font-weight: 800;
  }

  .model-selector {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .model-label {
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .model-select {
    background: var(--glass-bg);
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 3px 24px 3px 8px;
    cursor: pointer;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23888'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 7px center;
    transition: border-color var(--transition-fast), color var(--transition-fast);
  }
  .model-select:hover { border-color: rgba(255,107,53,0.4); color: var(--text-primary); }
  .model-select:focus { outline: none; border-color: var(--accent); }
</style>
