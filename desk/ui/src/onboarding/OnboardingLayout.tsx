import React from 'react';
import { useNova } from '../context/NovaStateContext';

interface OnboardingLayoutProps {
  children: React.ReactNode;
  title?: string;
  subtitle?: string;
}

export const OnboardingLayout: React.FC<OnboardingLayoutProps> = ({ children, title, subtitle }) => {
  const { theme } = useNova();

  return (
    <div 
      data-nova-blocking-overlay="onboarding-v2" 
      className="fixed inset-0 z-[60] flex items-center justify-center p-4 backdrop-blur-3xl animate-fade-in"
      style={{ backgroundColor: 'rgba(0,0,0,0.4)' }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className="w-full max-w-xl p-8 rounded-[32px] border shadow-2xl space-y-6 max-h-[90vh] overflow-y-auto relative transition-all duration-500"
        style={{ 
          backgroundColor: theme.palette.bgElevated, 
          borderColor: theme.palette.glassBorder, 
          boxShadow: `0 40px 80px -20px ${theme.palette.ambientShadow}, inset 0 1px 1px 0 ${theme.palette.glassHighlight}` 
        }}
      >
        <div className="flex flex-col items-center text-center space-y-2 mb-2">
          <div className="text-[10px] font-bold tracking-[0.4em] uppercase opacity-40 mb-2" style={{ color: theme.palette.textPrimary }}>
            Project Nova
          </div>
          {title && (
            <h2 className="text-2xl font-semibold tracking-tight" style={{ color: theme.palette.textPrimary }}>
              {title}
            </h2>
          )}
          {subtitle && (
            <p className="text-sm leading-relaxed max-w-sm" style={{ color: theme.palette.textSecondary }}>
              {subtitle}
            </p>
          )}
        </div>

        <div className="space-y-6">
          {children}
        </div>
      </div>
    </div>
  );
};

export const OnboardingButton: React.FC<{ 
  onClick?: () => void; 
  disabled?: boolean; 
  variant?: 'primary' | 'secondary';
  children: React.ReactNode;
  className?: string;
}> = ({ onClick, disabled, variant = 'primary', children, className = '' }) => {
  const { theme } = useNova();
  
  const isPrimary = variant === 'primary';
  
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`w-full py-3.5 rounded-2xl text-sm font-medium transition-all duration-200 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed active:scale-[0.98] ${className}`}
      style={{ 
        backgroundColor: isPrimary ? theme.palette.accent : 'transparent', 
        color: isPrimary ? (theme.isDark ? theme.palette.bgBase : '#fff') : theme.palette.textPrimary,
        borderColor: isPrimary ? theme.palette.accentBorder : theme.palette.glassBorder,
        borderWidth: '1px'
      }}
    >
      {children}
    </button>
  );
};

export const OnboardingInput: React.FC<React.InputHTMLAttributes<HTMLInputElement>> = (props) => {
  const { theme } = useNova();
  return (
    <input
      {...props}
      className={`w-full text-sm p-3.5 rounded-2xl border outline-none transition-all duration-200 focus:ring-2 focus:ring-offset-2 focus:ring-offset-transparent ${props.className || ''}`}
      style={{ 
        borderColor: theme.palette.glassBorder, 
        color: theme.palette.textPrimary, 
        backgroundColor: theme.palette.glassSurface,
        // Using accent for focus ring if we were using tailwind ring-color, 
        // but since we're using style, we'll just handle it with CSS if needed or keep it simple.
      }}
    />
  );
};
