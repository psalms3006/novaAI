import React from 'react';
import { OnboardingLayout, OnboardingButton } from './OnboardingLayout';
import { useNova } from '../context/NovaStateContext';
import { type SetupStep } from '../nova/lifecycle';

export const SuccessStep: React.FC<{ steps: SetupStep[]; onDone: () => void }> = ({ steps, onDone }) => {
  const { theme } = useNova();

  return (
    <OnboardingLayout 
      title="You're all set!" 
      subtitle="NOVA is now configured and ready to assist you."
    >
      <div className="space-y-6 pt-2">
        <div className="flex justify-center">
          <div 
            className="w-20 h-20 rounded-full flex items-center justify-center animate-bounce"
            style={{ backgroundColor: `${theme.palette.accent}15`, border: `1px solid ${theme.palette.accent}30` }}
          >
            <i className="fa-solid fa-check text-2xl" style={{ color: theme.palette.accent }} />
          </div>
        </div>

        <div className="space-y-3">
          <label className="text-[10px] font-bold tracking-widest opacity-40 ml-1 uppercase" style={{ color: theme.palette.textPrimary }}>Setup Summary</label>
          <div className="space-y-2 max-h-48 overflow-y-auto pr-2 custom-scrollbar">
            {steps.map((s) => (
              <div key={s.id} className="flex items-center gap-3 p-3 rounded-xl border" style={{ borderColor: theme.palette.glassBorder, backgroundColor: theme.palette.glassSurface }}>
                <i className={`fa-solid ${s.ok ? 'fa-circle-check text-green-500' : 'fa-circle-info text-yellow-500'} text-xs`} />
                <div className="flex-1">
                  <div className="text-[11px] font-medium" style={{ color: theme.palette.textPrimary }}>{s.label}</div>
                  {s.detail && <div className="text-[10px] opacity-50" style={{ color: theme.palette.textSecondary }}>{s.detail}</div>}
                </div>
              </div>
            ))}
          </div>
        </div>

        <OnboardingButton onClick={onDone}>
          Open NOVA
        </OnboardingButton>
      </div>
    </OnboardingLayout>
  );
};
