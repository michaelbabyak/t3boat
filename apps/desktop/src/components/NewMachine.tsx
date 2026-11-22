import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { DeskPreset, UiState } from "../types";
import { cleanName, hourly, NAME_RE, suggestName } from "../machine";
import { Icon } from "./Icons";
import { ProjectList, useLocalProjects } from "./ProjectPicker";
import { SubHeader } from "./SubHeader";

interface Props {
  state: UiState;
  onNameChange: (name: string) => void;
  onCreate: (
    name: string,
    size: string,
    environment: string | null,
    snapshot: string | null,
    projects: string[],
  ) => void;
  onBack: () => void;
}

const SLUG_RE = /^[A-Za-z0-9._-]+$/;

export function NewMachine({ state, onNameChange, onCreate, onBack }: Props) {
  const taken = state.machines.map((m) => m.name);
  const [presetId, setPresetId] = useState<string>("custom");
  const [name, setName] = useState(() => suggestName(taken));
  // Boat's own default: room for builds and tests without the large price.
  const [size, setSize] = useState(() => (state.sizes.some((s) => s.id === "default") ? "default" : (state.sizes[0]?.id ?? "")));
  const [environment, setEnvironment] = useState("");
  const [snapshot, setSnapshot] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const projects = useLocalProjects();
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  const syncPreset = (nextEnv: string, nextSnap: string) => {
    const e = nextEnv.trim() || null;
    const s = nextSnap.trim() || null;
    const matched = state.presets.find((p) => p.id !== "custom" && p.environment === e && p.snapshot === s);
    setPresetId(matched ? matched.id : "custom");
  };

  const applyPreset = (preset: DeskPreset) => {
    setPresetId(preset.id);
    setSize(preset.size);
    setEnvironment(preset.environment ?? "");
    setSnapshot(preset.snapshot ?? "");
    if (preset.defaultName) {
      setName(taken.includes(preset.defaultName) ? `${preset.defaultName}-2` : preset.defaultName);
    } else {
      setName(suggestName(taken));
    }
  };

  useEffect(() => {
    input.current?.select();
  }, []);
  useEffect(() => onNameChange(name), [name, onNameChange]);

  const envTrim = environment.trim();
  const snapTrim = snapshot.trim();
  const envProblem = envTrim && !SLUG_RE.test(envTrim) ? "Letters, digits, dots, underscores or dashes only" : null;
  const snapProblem = snapTrim && !SLUG_RE.test(snapTrim) ? "Letters, digits, dots, underscores or dashes only" : null;

  const problem = !name
    ? "Give it a name"
    : !NAME_RE.test(name)
      ? "Start with a letter or number"
      : taken.includes(name)
        ? "You already have a machine with this name"
        : null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!problem && !envProblem && !snapProblem && size) {
      onCreate(name, size, envTrim || null, snapTrim || null, picked);
    }
  };

  return (
    <form className="view" onSubmit={submit}>
      <SubHeader title="New machine" onBack={onBack} />
      <div className="view-body">
        {state.presets.length ? (
          <fieldset className="field">
            <legend className="caption">office preset</legend>
            <div className="presets" role="radiogroup">
              {state.presets.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  role="radio"
                  aria-checked={presetId === p.id}
                  className={`preset${presetId === p.id ? " is-on" : ""}`}
                  onClick={() => applyPreset(p)}
                >
                  <span className="preset-name">{p.label}</span>
                  <span className="preset-spec">{p.summary}</span>
                </button>
              ))}
            </div>
          </fieldset>
        ) : null}

        <div className="field">
          <label className="caption" htmlFor="machine-name">
            name
          </label>
          <span className="prompt-input">
            <span className="prompt-sign" aria-hidden>
              $
            </span>
            <input
              id="machine-name"
              ref={input}
              aria-describedby="machine-name-hint"
              value={name}
              onChange={(e) => setName(cleanName(e.target.value))}
              spellCheck={false}
              autoComplete="off"
              aria-invalid={!!problem}
            />
          </span>
          <span id="machine-name-hint" className={`hint${problem && name ? " is-error" : ""}`}>
            {problem ?? "Shows up under this name in T3 Code"}
          </span>
        </div>

        <div className="field-row">
          <div className="field">
            <label className="caption" htmlFor="machine-env">
              boat environment
            </label>
            <span className="prompt-input prompt-input-sm">
              <input
                id="machine-env"
                list="boat-envs"
                placeholder="base (default)"
                value={environment}
                onChange={(e) => {
                  const v = e.target.value;
                  setEnvironment(v);
                  syncPreset(v, snapshot);
                }}
                spellCheck={false}
                autoComplete="off"
                aria-invalid={!!envProblem}
              />
            </span>
            <datalist id="boat-envs">
              {state.environments.map((env) => (
                <option key={env} value={env} />
              ))}
            </datalist>
            {envProblem ? <span className="hint is-error">{envProblem}</span> : null}
          </div>

          <div className="field">
            <label className="caption" htmlFor="machine-snapshot">
              base snapshot
            </label>
            <span className="prompt-input prompt-input-sm">
              <input
                id="machine-snapshot"
                placeholder="none (clean)"
                value={snapshot}
                onChange={(e) => {
                  const v = e.target.value;
                  setSnapshot(v);
                  syncPreset(environment, v);
                }}
                spellCheck={false}
                autoComplete="off"
                aria-invalid={!!snapProblem}
              />
            </span>
            {snapProblem ? <span className="hint is-error">{snapProblem}</span> : null}
          </div>
        </div>

        <fieldset className="field">
          <legend className="caption">size</legend>
          <div className="sizes" role="radiogroup">
            {state.sizes.map((s) => (
              <label key={s.id} className={`size${size === s.id ? " is-on" : ""}`}>
                <input type="radio" name="size" value={s.id} checked={size === s.id} onChange={() => setSize(s.id)} />
                <span className="size-name">{s.id}</span>
                <span className="size-spec">{s.spec}</span>
                <span className="size-price">{hourly(s.hourly)}</span>
              </label>
            ))}
          </div>
          <span className="hint">
            Billed only while awake · sleeps 5 min after agents finish · resize anytime
          </span>
        </fieldset>

        <fieldset className="field">
          <legend className="caption">projects</legend>
          <div className="pick pick-checklist" role="group" aria-label="Projects to add">
            <div className="pick-scroll">
              <ProjectList
                projects={projects}
                shown={projects.list ?? []}
                query=""
                option={(p) => {
                  const on = picked.includes(p.id);
                  // One clone per repository: another project of the same repo is already picked.
                  const sameRepo = !on && (projects.list ?? []).some((o) => o.repoKey === p.repoKey && picked.includes(o.id));
                  const flip = () => !sameRepo && toggle(p.id);
                  return {
                    role: "checkbox",
                    tabIndex: 0,
                    "aria-checked": on,
                    "aria-disabled": sameRepo,
                    className: `pick-option${on ? " is-active" : ""}${sameRepo ? " is-done" : ""}`,
                    onClick: flip,
                    onKeyDown: (e: KeyboardEvent) => {
                      if (e.key === " " || e.key === "Enter") {
                        e.preventDefault();
                        flip();
                      }
                    },
                    badge: on ? (
                      <span className="line-ok">
                        <Icon name="check" size={10} />
                      </span>
                    ) : null,
                  };
                }}
              />
            </div>
          </div>
          <span className="hint">Optional · cloned from git once it's up, .env files included</span>
        </fieldset>
      </div>
      <div className="view-foot">
        <button type="submit" className="btn btn-solid btn-lg" disabled={!!problem || !!envProblem || !!snapProblem || !size}>
          Launch machine
        </button>
        <p className="foot-note">
          Takes about a minute{picked.length ? `, then ${picked.length} ${picked.length === 1 ? "project" : "projects"}` : ""}. T3 Code
          will ask to add it.
        </p>
      </div>
    </form>
  );
}
