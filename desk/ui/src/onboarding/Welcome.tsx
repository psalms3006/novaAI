import React from 'react';
import { OnboardingLayout, OnboardingButton } from './OnboardingLayout';
import { useNova } from '../context/NovaStateContext';

export const Welcome: React.FC<{ onContinue: () => void }> = ({ onContinue }) => {
  const { theme } = useNova();

  return (
    <OnboardingLayout 
      title="Welcome to NOVA" 
      subtitle="Your personal AI, built to work with you and around the way you work."
    >
      <div className="flex flex-col items-center space-y-6">
        <div 
          className="w-24 h-24 rounded-full flex items-center justify-center animate-pulse"
          style={{ backgroundColor: `${theme.palette.accent}15`, border: `1px solid ${theme.palette.accent}30` }}
        >
          <i className="fa-solid fa-sparkles text-3xl" style={{ color: theme.palette.accent }} />
        </div>
        
        <div className="text-center space-y-4">
          <p className="text-sm opacity-80 leading-relaxed" style={{ color: theme.palette.textSecondary }}>
            Let's get everything ready. We'll set up your account, personalize your experience, and configure your computer for the best results.
          </p>
        </div>

        <OnboardingButton onClick={onContinue}>
          Get Started
        </OnboardingButton>
      </div>
    </OnboardingLayout>
  );
};
