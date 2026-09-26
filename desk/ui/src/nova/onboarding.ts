// Whether to show the first-run setup screen. Pure and dependency-free so the
// test suite can run this exact file under Node (tests/test_onboarding_*.py).
//
// The rule, learned the hard way: absence of information is not evidence of
// a new user. /api/status can time out while the brain is still starting
// (around twenty seconds on a slow machine), and treating "no answer yet" as
// "never set up" put the welcome screen up on every slow start -- including
// for people who had entered their key several times. So the screen appears
// only when the backend has affirmatively said this person is not set up.

export interface AuthStatus {
  mode?: string;
  onboarded?: boolean;
  has_credential?: boolean;
  cloud_configured?: boolean;
  byok_present?: boolean;
  byok_masked?: string;
  chosen_offline?: boolean;
  credential_warning?: string;
  cloud_error?: string;
}

export function shouldShowOnboarding(status: { auth?: AuthStatus | null } | null | undefined): boolean {
  if (!status) return false;
  const a = status.auth;
  if (!a || typeof a !== 'object') return false;
  if (a.onboarded === true || a.has_credential === true) return false;
  // Both flags must be present and false. Missing means "not answered yet".
  if (a.onboarded === undefined && a.has_credential === undefined) return false;
  return !(a.onboarded || a.has_credential);
}
