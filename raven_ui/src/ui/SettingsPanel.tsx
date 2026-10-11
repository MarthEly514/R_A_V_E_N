import { useEffect, useState } from 'react';
import { useSettings, type Theme } from '../useSettings';
import type { ModelConfig, UserConfig } from '../useRaven';

interface SettingsPanelProps {
  onClose: () => void;
  connected: boolean;
  modelConfig: ModelConfig | null;
  modelsByProvider: Record<string, string[]>;
  modelsLoading: boolean;
  modelsError: string | null;
  getModelConfig: () => void;
  listModels: (provider: string) => void;
  setModel: (provider: string, name: string) => void;
  userConfig: UserConfig | null;
  getUserConfig: () => void;
  setApiKey: (provider: string, key: string) => void;
  setUserName: (name: string) => void;
}

const THEME_OPTIONS: { value: Theme; label: string }[] = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
];

// Only NVIDIA has a listable catalog today (raven/server.py's list_models);
// OpenRouter's model is still typed in directly, same as always.
const PROVIDER_OPTIONS: { value: string; label: string }[] = [
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'nvidia', label: 'NVIDIA' },
];

/**
 * A small settings dialog: appearance (the "mode button" -- Light/Dark/System),
 * the model provider + model picker (OpenRouter or NVIDIA's free NIM API --
 * see raven/llm_provider.py's NvidiaProvider), plus a few other options that
 * belong somewhere central rather than scattered across stages (voice's
 * auto-send used to be an inline checkbox on VoiceStage itself; it lives here
 * now instead).
 */
function SettingsPanel({
  onClose, connected, modelConfig, modelsByProvider, modelsLoading, modelsError,
  getModelConfig, listModels, setModel, userConfig, getUserConfig, setApiKey, setUserName,
}: SettingsPanelProps) {
  const { theme, setTheme, voiceAutoSend, setVoiceAutoSend, reduceMotion, setReduceMotion, showStats, setShowStats } =
    useSettings();

  // Local, uncommitted picks -- "Use this model" is what actually applies
  // them (setModel), so browsing providers/models never changes anything
  // live until the user confirms.
  const [pickedProvider, setPickedProvider] = useState<string | null>(null);
  const [pickedModel, setPickedModel] = useState('');
  const [justSaved, setJustSaved] = useState(false);

  // The API key field is write-only -- user_config only ever reports
  // whether a key is SET per provider (has_key), never the value itself, so
  // this starts empty every time and is cleared again right after saving.
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [keyJustSaved, setKeyJustSaved] = useState(false);
  const [nameInput, setNameInput] = useState('');
  const [nameJustSaved, setNameJustSaved] = useState(false);

  const provider = pickedProvider ?? modelConfig?.provider ?? 'openrouter';
  const models = modelsByProvider[provider] ?? [];

  useEffect(() => {
    getModelConfig();
    getUserConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on open; both are stable useCallbacks
  }, []);

  useEffect(() => {
    if (userConfig) setNameInput(userConfig.name);
  }, [userConfig]);

  const hasKey = userConfig?.has_key?.[provider] ?? false;

  const saveApiKey = () => {
    if (!apiKeyInput.trim()) return;
    setApiKey(provider, apiKeyInput.trim());
    setApiKeyInput('');
    setKeyJustSaved(true);
  };

  const saveName = () => {
    setUserName(nameInput.trim());
    setNameJustSaved(true);
  };

  useEffect(() => {
    // Seed the text field with the current model once we know it, but only
    // for the provider that's actually active -- never overwrite a model the
    // user already typed/picked for a DIFFERENT provider they're browsing.
    if (modelConfig && pickedProvider === null) setPickedModel(modelConfig.name);
  }, [modelConfig, pickedProvider]);

  useEffect(() => {
    if (provider === 'nvidia' && connected) listModels('nvidia');
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-fetch when the PROVIDER selection changes
  }, [provider, connected]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const selectProvider = (value: string) => {
    setPickedProvider(value);
    setPickedModel(value === modelConfig?.provider ? modelConfig.name : '');
    setJustSaved(false);
    setApiKeyInput('');
    setKeyJustSaved(false);
  };

  const applyModel = () => {
    if (!pickedModel.trim()) return;
    setModel(provider, pickedModel.trim());
    setJustSaved(true);
  };

  const isCurrentChoice = modelConfig?.provider === provider && modelConfig?.name === pickedModel.trim();

  return (
    <div
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-strong/30 backdrop-blur-[2px]"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        className="flex max-h-[85vh] w-full max-w-md flex-col border border-ink bg-[var(--bg-2)] p-6 shadow-lg"
      >
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-[1.05rem] font-semibold text-ink">Settings</h2>
          <button
            onClick={onClose}
            aria-label="Close settings"
            className="cursor-pointer text-[1.2rem] leading-none text-ink-dim hover:text-ink"
          >
            ×
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          <section className="mb-6">
            <div className="mb-2.5 text-[0.78rem] text-ink-dim">Appearance</div>
            <div className="flex gap-2">
              {THEME_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setTheme(opt.value)}
                  aria-pressed={theme === opt.value}
                  className={`flex-1 cursor-pointer border px-3 py-2 text-[0.82rem] font-semibold transition-colors duration-150 ${
                    theme === opt.value ? 'border-strong bg-strong text-onstrong' : 'border-ink text-ink hover:bg-ink/10'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </section>

          <section className="mb-6">
            <div className="mb-2.5 text-[0.78rem] text-ink-dim">Model</div>
            {modelConfig && (
              <div className="mb-2.5 text-[0.78rem] text-ink-dim">
                Currently: <span className="text-ink">{modelConfig.name}</span> ({modelConfig.provider})
              </div>
            )}
            <div className="mb-2.5 flex gap-2">
              {PROVIDER_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => selectProvider(opt.value)}
                  aria-pressed={provider === opt.value}
                  className={`flex-1 cursor-pointer border px-3 py-2 text-[0.82rem] font-semibold transition-colors duration-150 ${
                    provider === opt.value ? 'border-strong bg-strong text-onstrong' : 'border-ink text-ink hover:bg-ink/10'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>

            <div className="mb-3">
              <div className="mb-1.5 flex items-center justify-between">
                <span className="text-[0.78rem] text-ink-dim">
                  API key{' '}
                  {hasKey ? (
                    <span className="text-ink">(set)</span>
                  ) : (
                    <span className="text-danger">(not set)</span>
                  )}
                </span>
              </div>
              <div className="flex gap-2">
                <input
                  type="password"
                  value={apiKeyInput}
                  onChange={(e) => {
                    setApiKeyInput(e.target.value);
                    setKeyJustSaved(false);
                  }}
                  placeholder={hasKey ? 'Paste a new key to replace it' : `Paste your ${provider} API key`}
                  autoComplete="off"
                  className="flex-1 border border-line bg-panel px-3 py-2 text-[0.85rem] outline-none focus:border-ink"
                />
                <button
                  onClick={saveApiKey}
                  disabled={!connected || !apiKeyInput.trim()}
                  className="cursor-pointer border border-ink px-3 py-2 text-[0.82rem] font-semibold text-ink hover:bg-ink/10 disabled:cursor-not-allowed disabled:opacity-45"
                >
                  {keyJustSaved ? 'Saved' : 'Save'}
                </button>
              </div>
            </div>

            {provider === 'nvidia' ? (
              <>
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="text-[0.78rem] text-ink-dim">
                    {modelsError
                      ? 'Could not load models'
                      : modelsLoading
                        ? 'Loading models...'
                        : `${models.length} model${models.length === 1 ? '' : 's'} available`}
                  </span>
                  <button
                    onClick={() => listModels('nvidia')}
                    disabled={!connected || modelsLoading}
                    className="cursor-pointer text-[0.78rem] text-ink-dim underline hover:text-ink disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    Refresh
                  </button>
                </div>
                {modelsError ? (
                  <div className="mb-2 border border-danger px-2.5 py-2 text-[0.78rem] text-danger">{modelsError}.</div>
                ) : (
                  <select
                    value={models.includes(pickedModel) ? pickedModel : ''}
                    onChange={(e) => {
                      setPickedModel(e.target.value);
                      setJustSaved(false);
                    }}
                    disabled={models.length === 0}
                    className="mb-2 w-full border border-line bg-panel px-3 py-2 text-[0.85rem] outline-none focus:border-ink disabled:cursor-not-allowed disabled:opacity-45"
                  >
                    <option value="" disabled>
                      {models.length === 0 ? 'No models loaded yet' : 'Select a model'}
                    </option>
                    {models.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                )}
              </>
            ) : (
              <input
                value={pickedModel}
                onChange={(e) => {
                  setPickedModel(e.target.value);
                  setJustSaved(false);
                }}
                placeholder="e.g. openrouter/free"
                className="mb-2 w-full border border-line bg-panel px-3 py-2 text-[0.85rem] outline-none focus:border-ink"
              />
            )}

            <button
              onClick={applyModel}
              disabled={!connected || !pickedModel.trim() || isCurrentChoice}
              className="w-full cursor-pointer bg-strong px-3 py-2 text-[0.82rem] font-semibold text-onstrong disabled:cursor-not-allowed disabled:opacity-45"
            >
              {justSaved && isCurrentChoice ? 'Applied' : 'Use this model'}
            </button>
          </section>

          <section className="mb-6">
            <div className="mb-2.5 text-[0.78rem] text-ink-dim">Your name</div>
            <div className="flex gap-2">
              <input
                value={nameInput}
                onChange={(e) => {
                  setNameInput(e.target.value);
                  setNameJustSaved(false);
                }}
                placeholder="What should R.A.V.E.N call you?"
                className="flex-1 border border-line bg-panel px-3 py-2 text-[0.85rem] outline-none focus:border-ink"
              />
              <button
                onClick={saveName}
                disabled={!connected || nameInput.trim() === (userConfig?.name ?? '')}
                className="cursor-pointer border border-ink px-3 py-2 text-[0.82rem] font-semibold text-ink hover:bg-ink/10 disabled:cursor-not-allowed disabled:opacity-45"
              >
                {nameJustSaved ? 'Saved' : 'Save'}
              </button>
            </div>
          </section>

          <section className="flex flex-col gap-3">
            <div className="text-[0.78rem] text-ink-dim">General</div>
            <SettingRow
              label="Reduce motion"
              hint="Stops the Voice stage's animated orb"
              checked={reduceMotion}
              onChange={setReduceMotion}
            />
            <SettingRow
              label="Show model & stats"
              hint='The "Model & Stats" block in the right sidebar'
              checked={showStats}
              onChange={setShowStats}
            />
            <SettingRow
              label="Send voice automatically"
              hint="Skip reviewing the transcript before it's sent"
              checked={voiceAutoSend}
              onChange={setVoiceAutoSend}
            />
          </section>
        </div>
      </div>
    </div>
  );
}

function SettingRow({
  label, hint, checked, onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-start justify-between gap-4">
      <span className="flex flex-col">
        <span className="text-[0.88rem] text-ink">{label}</span>
        <span className="text-[0.75rem] text-ink-dim">{hint}</span>
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 accent-ink"
      />
    </label>
  );
}

export default SettingsPanel;
