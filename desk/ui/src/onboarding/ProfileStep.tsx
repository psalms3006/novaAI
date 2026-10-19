import React, { useState } from 'react';
import { OnboardingLayout, OnboardingButton, OnboardingInput } from './OnboardingLayout';
import { saveProfile, type Lifecycle } from '../nova/lifecycle';
import { useNova } from '../context/NovaStateContext';

const ROLES = [
  { id: 'student', label: 'Student', icon: 'fa-graduation-cap' },
  { id: 'developer', label: 'Developer', icon: 'fa-code' },
  { id: 'researcher', label: 'Researcher', icon: 'fa-microscope' },
  { id: 'business_owner', label: 'Business Owner', icon: 'fa-briefcase' },
  { id: 'professional', label: 'Professional', icon: 'fa-user-tie' },
  { id: 'other', label: 'Other', icon: 'fa-ellipsis' },
];

export const ProfileStep: React.FC<{ lc: Lifecycle; onDone: () => void }> = ({ lc, onDone }) => {
  const { theme } = useNova();
  const p = lc.instance?.profile || {};
  const [name, setName] = useState(p.preferred_name || '');
  const [role, setRole] = useState(p.role || '');
  const [about, setAbout] = useState(p.about || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (skip: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await saveProfile(skip ? {} : { preferred_name: name.trim(), role, about: about.trim() });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <OnboardingLayout 
      title="Personalize your experience" 
      subtitle="Tell NOVA a little about yourself so she can better assist you."
    >
      <div className="space-y-5">
        <div className="space-y-1.5">
          <label className="text-xs font-semibold opacity-70 ml-1" style={{ color: theme.palette.textPrimary }}>Preferred Name</label>
          <OnboardingInput 
            placeholder="How should NOVA address you?" 
            value={name} 
            onChange={(e) => setName(e.target.value)} 
            autoFocus 
          />
        </div>

        <div className="space-y-2">
          <label className="text-xs font-semibold opacity-70 ml-1" style={{ color: theme.palette.textPrimary }}>What best describes you?</label>
          <div className="grid grid-cols-2 gap-2">
            {ROLES.map((r) => {
              const active = role === r.id;
              return (
                <button
                  key={r.id}
                  onClick={() => setRole(active ? '' : r.id)}
                  className="flex items-center gap-3 p-3 rounded-2xl border text-left transition-all duration-200 cursor-pointer"
                  style={{ 
                    backgroundColor: active ? `${theme.palette.accent}10` : 'transparent',
                    borderColor: active ? theme.palette.accent : theme.palette.glassBorder,
                    boxShadow: active ? `0 0 0 1px ${theme.palette.accent}` : 'none'
                  }}
                >
                  <i className={`fa-solid ${r.icon} text-xs w-4 text-center`} style={{ color: active ? theme.palette.accent : theme.palette.textMuted }} />
                  <span className="text-[11px] font-medium" style={{ color: active ? theme.palette.textPrimary : theme.palette.textSecondary }}>{r.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-semibold opacity-70 ml-1" style={{ color: theme.palette.textPrimary }}>About you</label>
          <textarea 
            className="w-full text-sm p-3.5 rounded-2xl border outline-none h-24 resize-none transition-all duration-200"
            style={{ 
              borderColor: theme.palette.glassBorder, 
              color: theme.palette.textPrimary, 
              backgroundColor: theme.palette.glassSurface 
            }}
            placeholder="What you work on, interests, or how you plan to use NOVA..."
            value={about}
            onChange={(e) => setAbout(e.target.value)}
          />
        </div>

        {error && <p className="text-[11px] text-red-500">{error}</p>}

        <div className="flex gap-3 pt-2">
          <OnboardingButton variant="secondary" onClick={() => save(true)} disabled={busy} className="flex-1">
            Skip for now
          </OnboardingButton>
          <OnboardingButton onClick={() => save(false)} disabled={busy} className="flex-[2]">
            {busy ? 'Saving...' : 'Continue'}
          </OnboardingButton>
        </div>
      </div>
    </OnboardingLayout>
  );
};
