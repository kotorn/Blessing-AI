import React, { createContext, useContext, useEffect, useState } from 'react';
import { User, onAuthStateChanged } from 'firebase/auth';
import {
  auth,
  db,
  signInWithGoogle as fbSignIn,
  logOut as fbSignOut,
  testFirestoreConnection,
  recordAuditLog,
  handleFirestoreError,
  OperationType,
  getGoogleAccessToken,
} from '../lib/firebase';
import { doc, setDoc, query, collection, orderBy, limit, getDocs } from 'firebase/firestore';
import {
  exportBasketsToGoogleSheet,
  listGoogleDriveFiles,
  deleteDriveFile,
  GoogleDriveFile,
  SheetExportResult,
} from '../lib/workspace';

interface AuthContextType {
  user: User | null;
  loading: boolean;
  firestoreConnected: boolean;
  accessToken: string | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  cloudAudit: (action: string, symbol?: string, details?: string) => Promise<void>;
  saveCloudBasket: (basket: any) => Promise<void>;
  saveRiskSettings: (settings: {
    maxPortfolioLeverage: number;
    emergencyDrawdownPct: number;
    marginStressThresholdPct?: number;
    killSwitchActive?: boolean;
  }) => Promise<void>;
  exportToGoogleSheet: (baskets: any[], portfolio: any) => Promise<SheetExportResult>;
  fetchDriveFiles: () => Promise<GoogleDriveFile[]>;
  deleteDriveFile: (fileId: string, fileName: string) => Promise<boolean>;
  fetchAuditLogs: () => Promise<any[]>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [firestoreConnected, setFirestoreConnected] = useState<boolean>(false);
  const [tokenState, setTokenState] = useState<string | null>(getGoogleAccessToken());

  useEffect(() => {
    // 1. Verify Firestore connectivity on boot
    testFirestoreConnection().then((ok) => {
      setFirestoreConnected(ok);
      if (ok) {
        console.log('Firebase Firestore verified and connected');
      }
    });

    // 2. Auth state observer
    const unsubscribe = onAuthStateChanged(auth, (currentUser) => {
      setUser(currentUser);
      setTokenState(getGoogleAccessToken());
      setLoading(false);
    });

    return () => unsubscribe();
  }, []);

  const signIn = async () => {
    try {
      await fbSignIn();
      setTokenState(getGoogleAccessToken());
    } catch (err) {
      console.error('Sign in failed:', err);
    }
  };

  const signOut = async () => {
    try {
      await fbSignOut();
      setTokenState(null);
    } catch (err) {
      console.error('Sign out failed:', err);
    }
  };

  const cloudAudit = async (action: string, symbol?: string, details?: string) => {
    await recordAuditLog(action, symbol, details);
  };

  const exportToGoogleSheet = async (baskets: any[], portfolio: any): Promise<SheetExportResult> => {
    const currentToken = getGoogleAccessToken() || tokenState;
    if (!currentToken) {
      throw new Error('Google Workspace authentication required. Please sign in with your Google account.');
    }
    const result = await exportBasketsToGoogleSheet(currentToken, baskets, portfolio);
    await cloudAudit('GOOGLE_SHEETS_EXPORT', undefined, `Exported ${baskets.length} baskets to ${result.title}`);
    return result;
  };

  const fetchDriveFiles = async (): Promise<GoogleDriveFile[]> => {
    const currentToken = getGoogleAccessToken() || tokenState;
    if (!currentToken) {
      return [];
    }
    return await listGoogleDriveFiles(currentToken);
  };

  const deleteDriveFileAction = async (fileId: string, fileName: string): Promise<boolean> => {
    const currentToken = getGoogleAccessToken() || tokenState;
    if (!currentToken) {
      throw new Error('Google Workspace authentication required.');
    }
    const ok = await deleteDriveFile(currentToken, fileId);
    if (ok) {
      await cloudAudit('GOOGLE_DRIVE_DELETE', undefined, `Deleted file: ${fileName} (${fileId})`);
    }
    return ok;
  };

  const saveCloudBasket = async (basket: any) => {
    if (!user) return;
    const path = `users/${user.uid}/baskets/${basket.basket_id || basket.basketId}`;
    try {
      const basketRef = doc(db, 'users', user.uid, 'baskets', basket.basket_id || basket.basketId);
      await setDoc(
        basketRef,
        {
          basketId: basket.basket_id || basket.basketId,
          userId: user.uid,
          instrument: basket.instrument,
          direction: basket.direction,
          state: basket.state,
          gridDepth: basket.grid_depth ?? basket.gridDepth ?? 0,
          maxGridLevels: basket.max_grid_levels ?? basket.maxGridLevels ?? 5,
          totalSize: basket.total_size ?? basket.totalSize ?? 0,
          averageEntry: basket.average_entry ?? basket.averageEntry ?? 0,
          netPnl: basket.net_pnl ?? basket.netPnl ?? 0,
          updatedAt: new Date().toISOString(),
        },
        { merge: true }
      );
    } catch (error) {
      handleFirestoreError(error, OperationType.WRITE, path);
    }
  };

  const saveRiskSettings = async (settings: {
    maxPortfolioLeverage: number;
    emergencyDrawdownPct: number;
    marginStressThresholdPct?: number;
    killSwitchActive?: boolean;
  }) => {
    if (!user) return;
    const path = `users/${user.uid}/settings/risk`;
    try {
      const riskRef = doc(db, 'users', user.uid, 'settings', 'risk');
      await setDoc(
        riskRef,
        {
          userId: user.uid,
          maxPortfolioLeverage: settings.maxPortfolioLeverage,
          emergencyDrawdownPct: settings.emergencyDrawdownPct,
          marginStressThresholdPct: settings.marginStressThresholdPct ?? 30.0,
          killSwitchActive: settings.killSwitchActive ?? false,
          updatedAt: new Date().toISOString(),
        },
        { merge: true }
      );
    } catch (error) {
      handleFirestoreError(error, OperationType.WRITE, path);
    }
  };

  const fetchAuditLogs = async () => {
    if (!user) return [];
    try {
      const q = query(
        collection(db, 'users', user.uid, 'audit_logs'),
        orderBy('timestamp', 'desc'),
        limit(100)
      );
      const snapshot = await getDocs(q);
      return snapshot.docs.map(doc => doc.data());
    } catch (err) {
      console.warn('Failed to fetch audit logs:', err);
      return [];
    }
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        firestoreConnected,
        accessToken: tokenState,
        signIn,
        signOut,
        cloudAudit,
        saveCloudBasket,
        saveRiskSettings,
        exportToGoogleSheet,
        fetchDriveFiles,
        deleteDriveFile: deleteDriveFileAction,
        fetchAuditLogs,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
