/**
 * Firebase Client Initialization & Utilities for Blessing AI
 */

import { initializeApp, getApps, getApp } from 'firebase/app';
import {
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signOut,
  User,
} from 'firebase/auth';
import {
  getFirestore,
  doc,
  getDocFromServer,
  setDoc,
} from 'firebase/firestore';

// Import client configuration
import firebaseConfig from '../../firebase-applet-config.json';

// Initialize Firebase App instance idempotently
const app = getApps().length === 0 ? initializeApp(firebaseConfig) : getApp();

// Initialize Firestore with explicit databaseId
export const db = getFirestore(app, (firebaseConfig as any).firestoreDatabaseId);
export const auth = getAuth(app);

/** Return the short-lived Firebase ID token used for backend identity checks. */
export async function getFirebaseIdToken(): Promise<string | null> {
  return auth.currentUser ? auth.currentUser.getIdToken() : null;
}

// Configure Google Auth Provider only for Drive/Sheets. BigQuery browser
// identity uses Firebase ID tokens and never this OAuth access token.
export const googleProvider = new GoogleAuthProvider();
googleProvider.addScope('https://www.googleapis.com/auth/drive.file');
googleProvider.addScope('https://www.googleapis.com/auth/spreadsheets');
googleProvider.addScope('https://www.googleapis.com/auth/drive.readonly');
googleProvider.setCustomParameters({ prompt: 'select_account' });

// In-memory token cache (never stored in localStorage)
let cachedAccessToken: string | null = null;

export function getGoogleAccessToken(): string | null {
  return cachedAccessToken;
}

export function setGoogleAccessToken(token: string | null): void {
  cachedAccessToken = token;
}

export enum OperationType {
  CREATE = 'create',
  UPDATE = 'update',
  DELETE = 'delete',
  LIST = 'list',
  GET = 'get',
  WRITE = 'write',
}

export interface FirestoreErrorInfo {
  error: string;
  operationType: OperationType;
  path: string | null;
  authInfo: {
    userId?: string | null;
    email?: string | null;
    emailVerified?: boolean | null;
    isAnonymous?: boolean | null;
    tenantId?: string | null;
    providerInfo?: {
      providerId?: string | null;
      email?: string | null;
    }[];
  };
}

export function handleFirestoreError(
  error: unknown,
  operationType: OperationType,
  path: string | null
): never {
  const errInfo: FirestoreErrorInfo = {
    error: error instanceof Error ? error.message : String(error),
    authInfo: {
      userId: auth.currentUser?.uid || null,
      email: auth.currentUser?.email || null,
      emailVerified: auth.currentUser?.emailVerified || null,
      isAnonymous: auth.currentUser?.isAnonymous || null,
      tenantId: auth.currentUser?.tenantId || null,
      providerInfo:
        auth.currentUser?.providerData?.map((p) => ({
          providerId: p.providerId,
          email: p.email,
        })) || [],
    },
    operationType,
    path,
  };
  console.error('Firestore Error:', JSON.stringify(errInfo));
  throw new Error(JSON.stringify(errInfo));
}

/**
 * Validates connection to the provisioned Firestore database
 */
export async function testFirestoreConnection(): Promise<boolean> {
  try {
    await getDocFromServer(doc(db, 'test', 'connection'));
    return true;
  } catch (error: any) {
    if (error instanceof Error && error.message.includes('the client is offline')) {
      console.warn('Firestore offline:', error.message);
      return false;
    }
    // Any other response (including document not found or permission check) means connection reached server
    return true;
  }
}

// In-flight promise to deduplicate concurrent popup requests
let activeSignInPromise: Promise<User | null> | null = null;

/**
 * Sign In with Google popup with concurrent request deduplication and graceful cancellation handling
 */
export async function signInWithGoogle(): Promise<User | null> {
  if (activeSignInPromise) {
    return activeSignInPromise;
  }

  activeSignInPromise = (async () => {
    try {
      const result = await signInWithPopup(auth, googleProvider);
      // Capture OAuth access token for Google Drive & Google Sheets API calls
      const credential = GoogleAuthProvider.credentialFromResult(result);
      if (credential?.accessToken) {
        cachedAccessToken = credential.accessToken;
      }
      // Sync/create user profile in Firestore
      if (result.user) {
        const userRef = doc(db, 'users', result.user.uid);
        try {
          await setDoc(
            userRef,
            {
              uid: result.user.uid,
              email: result.user.email || '',
              displayName: result.user.displayName || 'Quant Trader',
              photoURL: result.user.photoURL || '',
              preferredVenue: 'binance_global',
              updatedAt: new Date().toISOString(),
            },
            { merge: true }
          );
        } catch (err) {
          console.warn('Could not save user profile (rules might restrict if new):', err);
        }
      }
      return result.user;
    } catch (error: any) {
      const errorCode = error?.code || '';
      // Gracefully handle cancelled or closed popup requests without polluting console.error
      if (
        errorCode === 'auth/cancelled-popup-request' ||
        errorCode === 'auth/popup-closed-by-user'
      ) {
        console.info('Google Sign-in popup cancelled or closed by user.');
        return null;
      }
      console.warn('Google Sign-in did not complete:', error?.message || error);
      throw error;
    } finally {
      activeSignInPromise = null;
    }
  })();

  return activeSignInPromise;
}

/**
 * Sign Out
 */
export async function logOut(): Promise<void> {
  cachedAccessToken = null;
  await signOut(auth);
}

/**
 * Record an audit log entry in Firestore for the current user
 */
export async function recordAuditLog(action: string, symbol?: string, details?: string): Promise<void> {
  if (!auth.currentUser) return;
  const logId = `log_${Date.now()}`;
  const logRef = doc(db, 'users', auth.currentUser.uid, 'audit_logs', logId);
  try {
    await setDoc(logRef, {
      logId,
      userId: auth.currentUser.uid,
      action,
      symbol: symbol || '',
      details: details || '',
      timestamp: new Date().toISOString(),
    });
  } catch (err) {
    console.warn('Failed to record audit log in Firestore:', err);
  }
}
