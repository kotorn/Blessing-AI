import { describe, expect, it, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Hoist mocks for Firebase Web SDK
const firebaseMocks = vi.hoisted(() => ({
  signInWithPopup: vi.fn(),
  signOut: vi.fn(),
  setDoc: vi.fn(),
  getApps: vi.fn(() => []),
  initializeApp: vi.fn(() => ({})),
  getFirestore: vi.fn(() => ({})),
  getAuth: vi.fn(() => ({ currentUser: null })),
  doc: vi.fn(),
}));

vi.mock('firebase/app', () => ({
  initializeApp: firebaseMocks.initializeApp,
  getApps: firebaseMocks.getApps,
  getApp: vi.fn(() => ({})),
}));

vi.mock('firebase/auth', () => ({
  getAuth: firebaseMocks.getAuth,
  GoogleAuthProvider: class {
    addScope = vi.fn();
    setCustomParameters = vi.fn();
    static credentialFromResult = vi.fn(() => ({ accessToken: 'mock-oauth-token' }));
  },
  signInWithPopup: firebaseMocks.signInWithPopup,
  signOut: firebaseMocks.signOut,
  onAuthStateChanged: vi.fn(),
}));

vi.mock('firebase/firestore', () => ({
  getFirestore: firebaseMocks.getFirestore,
  doc: firebaseMocks.doc,
  getDocFromServer: vi.fn(),
  setDoc: firebaseMocks.setDoc,
  getDoc: vi.fn(),
  collection: vi.fn(),
  getDocs: vi.fn(),
  onSnapshot: vi.fn(),
  query: vi.fn(),
  orderBy: vi.fn(),
  limit: vi.fn(),
  serverTimestamp: vi.fn(),
  deleteDoc: vi.fn(),
}));

import { signInWithGoogle } from '../src/lib/firebase';

describe('Firebase Auth popup concurrency & cancellation handling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('gracefully handles auth/cancelled-popup-request without throwing or logging console.error', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const consoleInfoSpy = vi.spyOn(console, 'info').mockImplementation(() => {});

    const cancelError: any = new Error('Firebase: Error (auth/cancelled-popup-request).');
    cancelError.code = 'auth/cancelled-popup-request';
    firebaseMocks.signInWithPopup.mockRejectedValueOnce(cancelError);

    const user = await signInWithGoogle();
    expect(user).toBeNull();
    // Verify console.error was NOT called for user cancellation
    expect(consoleErrorSpy).not.toHaveBeenCalled();
    expect(consoleInfoSpy).toHaveBeenCalledWith(
      expect.stringContaining('cancelled or closed by user')
    );

    consoleErrorSpy.mockRestore();
    consoleInfoSpy.mockRestore();
  });

  it('gracefully handles auth/popup-closed-by-user without throwing or logging console.error', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const consoleInfoSpy = vi.spyOn(console, 'info').mockImplementation(() => {});

    const closedError: any = new Error('Firebase: Error (auth/popup-closed-by-user).');
    closedError.code = 'auth/popup-closed-by-user';
    firebaseMocks.signInWithPopup.mockRejectedValueOnce(closedError);

    const user = await signInWithGoogle();
    expect(user).toBeNull();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
    expect(consoleInfoSpy).toHaveBeenCalledWith(
      expect.stringContaining('cancelled or closed by user')
    );

    consoleErrorSpy.mockRestore();
    consoleInfoSpy.mockRestore();
  });

  it('deduplicates concurrent signInWithGoogle calls to the exact same in-flight promise', async () => {
    let resolvePopup: (val: any) => void = () => {};
    const popupPromise = new Promise((resolve) => {
      resolvePopup = resolve;
    });

    firebaseMocks.signInWithPopup.mockReturnValueOnce(popupPromise);

    // Trigger two sign-in calls concurrently
    const call1 = signInWithGoogle();
    const call2 = signInWithGoogle();

    // signInWithPopup should only have been called once!
    expect(firebaseMocks.signInWithPopup).toHaveBeenCalledTimes(1);

    resolvePopup({
      user: {
        uid: 'test-user-123',
        email: 'kotorn@gmail.com',
        displayName: 'Quant Trader',
      },
    });

    const [user1, user2] = await Promise.all([call1, call2]);
    expect(user1).toEqual(user2);
    expect(user1?.uid).toBe('test-user-123');
  });

  it('verifies UI components guard against concurrent sign-in requests and display loading state', () => {
    const headerCode = readFileSync(resolve(process.cwd(), 'src/components/Header.tsx'), 'utf8');
    const statusBarCode = readFileSync(resolve(process.cwd(), 'src/app/StatusBar.tsx'), 'utf8');
    const modalCode = readFileSync(resolve(process.cwd(), 'src/components/GoogleWorkspaceModal.tsx'), 'utf8');
    const basketCode = readFileSync(resolve(process.cwd(), 'src/components/BasketManager.tsx'), 'utf8');

    // Header has disabled attribute and spinner during isSigningIn
    expect(headerCode).toContain('disabled={isSigningIn}');
    expect(headerCode).toContain('Signing In...');

    // StatusBar has disabled attribute and connecting state during isSigningIn
    expect(statusBarCode).toContain('disabled={isSigningIn}');
    expect(statusBarCode).toContain('Connecting...');

    // GoogleWorkspaceModal guards with isSigningIn
    expect(modalCode).toContain('disabled={isSigningIn}');

    // BasketManager guards quick export
    expect(basketCode).toContain('if (isSigningIn) return;');
  });
});
