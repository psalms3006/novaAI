import React, { useCallback, useEffect, useState } from 'react';
import { OnboardingLayout } from './OnboardingLayout';
import { getLifecycle, type Lifecycle } from '../nova/lifecycle';
import { useRuntime } from '../nova/runtime';
import { AccountStep } from './AccountStep';
import { VerifyStep } from './VerifyStep';
import { ProfileStep } from './ProfileStep';
import { DeviceSetup } from './DeviceSetup';

export const OnboardingEntry: React.FC = () => {
  const [lc, setLc] = useState<Lifecycle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const rt = useRuntime();

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getLifecycle();
      setLc(data);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (loading && !lc) return null; // Initial load

  if (error) {
    return (
      <OnboardingLayout title="Connection Error" subtitle={error}>
        <button 
          onClick={refresh}
          className="w-full py-3 rounded-xl border text-sm"
        >
          Try Again
        </button>
      </OnboardingLayout>
    );
  }

  if (!lc || !lc.accounts_enabled || lc.next === 'none') return null;

  const handleDone = async () => {
    await refresh();
    rt.refreshStatus();
  };

  switch (lc.next) {
    case 'sign_in':
      return <AccountStep lc={lc} onDone={handleDone} />;
    case 'verify_email':
      return <VerifyStep onDone={handleDone} />;
    case 'profile':
      return <ProfileStep lc={lc} onDone={handleDone} />;
    case 'device_setup':
      return <DeviceSetup onDone={handleDone} />;
    default:
      return null;
  }
};
