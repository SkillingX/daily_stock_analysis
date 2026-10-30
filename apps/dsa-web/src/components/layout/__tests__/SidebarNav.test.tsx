import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { SidebarNav } from '../SidebarNav';

const mockLogout = vi.fn().mockResolvedValue(undefined);
const mockGetAlphaSiftStatus = vi.fn().mockResolvedValue({ enabled: false, available: false, installSpecIsDefault: false });
const mockThemeToggle = vi.fn(({ collapsed }: { collapsed?: boolean }) => (
  <button type="button">{collapsed ? '切换主题(折叠)' : '切换主题'}</button>
));

const completionBadgeState = { value: true };

vi.mock('../../../contexts/AuthContext', () => ({
  useAuth: () => ({
    authEnabled: true,
    logout: mockLogout,
  }),
}));

vi.mock('../../../stores/agentChatStore', () => ({
  useAgentChatStore: (selector: (state: { completionBadge: boolean }) => unknown) =>
    selector({ completionBadge: completionBadgeState.value }),
}));

vi.mock('../../../api/alphasift', () => ({
  ALPHASIFT_CONFIG_CHANGED_EVENT: 'alphasift-config-changed',
  SYSTEM_CONFIG_CHANGED_EVENT: 'dsa-system-config-changed',
  alphasiftApi: {
    getStatus: () => mockGetAlphaSiftStatus(),
  },
}));

vi.mock('../../theme/ThemeToggle', () => ({
  ThemeToggle: (props: { collapsed?: boolean }) => mockThemeToggle(props),
}));

describe('SidebarNav', () => {
  it('hides the screening navigation item while AlphaSift is disabled', () => {
    mockGetAlphaSiftStatus.mockResolvedValueOnce({ enabled: false, available: false, installSpecIsDefault: false });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: '选股' })).not.toBeInTheDocument();
  });

  it('shows the screening navigation item when AlphaSift is enabled', async () => {
    mockGetAlphaSiftStatus.mockResolvedValueOnce({ enabled: true, available: false, installSpecIsDefault: false });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('link', { name: '选股' })).toHaveAttribute('href', '/screening');
  });

  it('orders visible navigation items with screening after supply chain when AlphaSift is enabled', async () => {
    mockGetAlphaSiftStatus.mockResolvedValueOnce({ enabled: true, available: false, installSpecIsDefault: false });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    await screen.findByRole('link', { name: '选股' });
    const hrefs = screen.getAllByRole('link').map((link) => link.getAttribute('href'));
    // 当前 NAV_ITEMS 可见顺序（SidebarNav.tsx）：chat/portfolio/decision-signals/
    // backtest/alerts/zhengxi 按需求隐藏（路由保留、菜单不出现）
    expect(hrefs).toEqual([
      '/',
      '/trend-compass',
      '/deep-research',
      '/fundamentals',
      '/financial-analysis',
      '/sector-analysis',
      '/policy-minesweeper',
      '/knowledge-base',
      '/chanlun',
      '/supply-chain',
      '/screening',
      '/usage',
      '/settings',
    ]);
  });

  it('refreshes the screening navigation item after any config save event', async () => {
    mockGetAlphaSiftStatus
      .mockResolvedValueOnce({ enabled: false, available: false, installSpecIsDefault: false })
      .mockResolvedValueOnce({ enabled: true, available: false, installSpecIsDefault: false });

    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: '选股' })).not.toBeInTheDocument();
    window.dispatchEvent(new Event('dsa-system-config-changed'));

    expect(await screen.findByRole('link', { name: '选股' })).toHaveAttribute('href', '/screening');
    await waitFor(() => expect(mockGetAlphaSiftStatus.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('keeps the hidden chat completion badge out of the menu even when completion is pending', () => {
    // chat 菜单入口按需求隐藏（路由保留）：completionBadge 无论真伪，
    // 菜单中不得出现徽标或「A股分析有新消息」标签。
    completionBadgeState.value = true;

    const { rerender } = render(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId('chat-completion-badge')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('A股分析有新消息')).not.toBeInTheDocument();
    // 直接处于隐藏路由时也不误现
    expect(screen.queryByRole('link', { name: 'A股分析' })).not.toBeInTheDocument();

    completionBadgeState.value = false;
    rerender(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId('chat-completion-badge')).not.toBeInTheDocument();
  });

  it('renders the collapsed theme toggle variant when the sidebar is collapsed', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav collapsed />
      </MemoryRouter>,
    );

    expect(mockThemeToggle).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'nav', collapsed: true }),
    );
    expect(screen.getByRole('button', { name: '切换主题(折叠)' })).toBeInTheDocument();
  });

  it('keeps the hidden alerts route out of the menu even on its own route', () => {
    // 告警入口按需求隐藏（路由保留）：菜单不出现，直接访问也不误现
    render(
      <MemoryRouter initialEntries={['/alerts']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: '告警' })).not.toBeInTheDocument();
    expect(
      screen.queryAllByRole('link').some((link) => link.getAttribute('href') === '/alerts')
    ).toBe(false);
  });

  it('keeps the hidden AI signals route out of the menu even on its own route', () => {
    // AI 建议入口按需求隐藏（路由保留）：菜单不出现，直接访问也不误现
    render(
      <MemoryRouter initialEntries={['/decision-signals']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole('link', { name: 'AI 建议' })).not.toBeInTheDocument();
    expect(
      screen.queryAllByRole('link').some((link) => link.getAttribute('href') === '/decision-signals')
    ).toBe(false);
  });

  it('opens the logout confirmation and confirms logout', async () => {
    render(
      <MemoryRouter initialEntries={['/chat']}>
        <SidebarNav />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole('button', { name: '退出' }));

    expect(await screen.findByRole('heading', { name: '退出登录' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认退出' }));
    expect(mockLogout).toHaveBeenCalled();
  });

  it('scrolls the menu internally when the viewport is shorter than the menu stack', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav variant="rail" />
      </MemoryRouter>,
    );

    const nav = screen.getByRole('navigation', { name: '主导航' });
    expect(nav.className).toContain('overflow-y-auto');
    expect(nav.className).toContain('flex-1');
    expect(nav.className).toContain('min-h-0');
  });

  it('clamps the menu height to the parent flex container so overflow-y-auto can take effect', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <div className="flex h-40 flex-col" data-testid="shell-aside">
          <SidebarNav variant="rail" />
        </div>
      </MemoryRouter>,
    );

    const inner = screen.getByTestId('shell-aside').firstElementChild;
    expect(inner?.className).toContain('h-full');
    expect(inner?.className).toContain('flex-col');

    const nav = screen.getByRole('navigation', { name: '主导航' });
    expect(nav.className).toContain('overflow-y-auto');
    expect(nav.className).toContain('flex-1');
    expect(nav.className).toContain('min-h-0');
  });

  it('uses a flexible min-height with vertical padding so labels do not clip when zoomed', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <SidebarNav variant="rail" />
      </MemoryRouter>,
    );

    const homeLink = screen.getByRole('link', { name: '首页' });
    expect(homeLink.className).toContain('min-h-[var(--nav-item-height)]');
    expect(homeLink.className).not.toMatch(/\bh-\[var\(--nav-item-height\)\]\b/);
    expect(homeLink.className).toContain('py-1.5');
    expect(homeLink.className).toContain('leading-tight');
  });
});
