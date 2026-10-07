import type React from 'react';
import { lazy, useEffect } from 'react';
import { BrowserRouter as Router, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ApiErrorAlert, Shell } from './components/common';
import {
  PageLoadingFallback,
  RouteOutletBoundary,
  StandaloneRouteBoundary,
} from './components/layout/RouteBoundary';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { UiLanguageProvider, useUiLanguage } from './contexts/UiLanguageContext';
import { useAgentChatStore } from './stores/agentChatStore';
import './App.css';

const HomePage = lazy(() => import('./pages/HomePage'));
const FundamentalsPage = lazy(() => import('./pages/FundamentalsPage'));
const FinancialAnalysisPage = lazy(() => import('./pages/FinancialAnalysisPage'));
const BacktestPage = lazy(() => import('./pages/BacktestPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const LoginPage = lazy(() => import('./pages/LoginPage'));
const NotFoundPage = lazy(() => import('./pages/NotFoundPage'));
const ChatPage = lazy(() => import('./pages/ChatPage'));
const ZhengxiChatPage = lazy(() => import('./pages/ZhengxiChatPage'));
const SupplyChainReportPage = lazy(() => import('./pages/SupplyChainReportPage'));
const DeepResearchPage = lazy(() => import('./pages/DeepResearchPage'));
const TrendCompassPage = lazy(() => import('./pages/TrendCompassPage'));
const PolicyMinesweeperPage = lazy(() => import('./pages/PolicyMinesweeperPage'));
const KnowledgeBasePage = lazy(() => import('./pages/KnowledgeBasePage'));

const ChanlunChatPage = lazy(() => import('./pages/ChanlunChatPage'));
const SectorAnalysisPage = lazy(() => import('./pages/SectorAnalysisPage'));
const PortfolioPage = lazy(() => import('./pages/PortfolioPage'));
const DecisionSignalsPage = lazy(() => import('./pages/DecisionSignalsPage'));
const AlertsPage = lazy(() => import('./pages/AlertsPage'));
const TokenUsagePage = lazy(() => import('./pages/TokenUsagePage'));
const StockScreeningPage = lazy(() => import('./pages/StockScreeningPage'));

const AppContent: React.FC = () => {
  const location = useLocation();
  const { authEnabled, loggedIn, isLoading, loadError, refreshStatus } = useAuth();
  const { t } = useUiLanguage();

  useEffect(() => {
    useAgentChatStore.getState().setCurrentRoute(location.pathname);
  }, [location.pathname]);

  if (isLoading) {
    return <PageLoadingFallback />;
  }

  if (loadError) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4">
        <div className="w-full max-w-lg">
          <ApiErrorAlert error={loadError} />
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => void refreshStatus()}
        >
          {t('common.retry')}
        </button>
      </div>
    );
  }

  if (authEnabled && !loggedIn) {
    if (location.pathname === '/login') {
      return (
        <StandaloneRouteBoundary>
          <LoginPage />
        </StandaloneRouteBoundary>
      );
    }
    const redirect = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?redirect=${redirect}`} replace />;
  }

  if (location.pathname === '/login') {
    return <Navigate to="/" replace />;
  }

  return (
    <Routes>
      <Route
        element={(
          <Shell>
            <RouteOutletBoundary />
          </Shell>
        )}
      >
        <Route path="/" element={<HomePage />} />
        <Route path="/trend-compass" element={<TrendCompassPage />} />
        <Route path="/deep-research" element={<DeepResearchPage />} />
        <Route path="/policy-minesweeper" element={<PolicyMinesweeperPage />} />
        <Route path="/knowledge-base" element={<KnowledgeBasePage />} />
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/fundamentals" element={<FundamentalsPage />} />
        <Route path="/financial-analysis" element={<FinancialAnalysisPage />} />
        <Route path="/sector-analysis" element={<SectorAnalysisPage />} />
        <Route path="/zhengxi" element={<ZhengxiChatPage />} />
        <Route path="/chanlun" element={<ChanlunChatPage />} />
        <Route path="/supply-chain" element={<SupplyChainReportPage />} />
        <Route path="/portfolio" element={<PortfolioPage />} />
        <Route path="/decision-signals" element={<DecisionSignalsPage />} />
        <Route path="/screening" element={<StockScreeningPage />} />
        <Route path="/backtest" element={<BacktestPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/usage" element={<TokenUsagePage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
};

const App: React.FC = () => {
  return (
    <UiLanguageProvider>
      <Router>
        <AuthProvider>
          <AppContent />
        </AuthProvider>
      </Router>
    </UiLanguageProvider>
  );
};

export default App;
