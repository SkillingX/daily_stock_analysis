import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ReportMarkdownBody } from '../ReportMarkdownBody';

describe('saved report formatting', () => {
  it('unwraps legacy font emphasis while preserving links, lists and tables', () => {
    const { container } = render(<ReportMarkdownBody content={'- <font color="#e03131">🔴 **经营模式**</font>：[公告](https://example.com)\n- 第二项\n\n| 指标 | 值 |\n| --- | --- |\n| ROE | 0 |'} />);
    expect(container.textContent).not.toContain('<font');
    expect(container.textContent).not.toContain('</font>');
    expect(container.querySelector('strong')?.textContent).toBe('经营模式');
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(container.querySelector('a')?.getAttribute('href')).toBe('https://example.com');
    expect(container.querySelector('table')).not.toBeNull();
  });
  it('keeps code examples literal and does not execute arbitrary HTML', () => {
    const { container } = render(<ReportMarkdownBody content={'`<font color="#e03131">example</font>`\n\n```html\n<font color="#e03131">example</font>\n```\n\n<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n<font color="#e03131" onclick="alert(1)">untrusted</font>\n\n[bad](javascript:alert(1))'} />);
    expect(container.querySelectorAll('code')).toHaveLength(2);
    expect(container.querySelector('code')?.textContent).toContain('<font');
    expect(container.querySelector('pre code')?.textContent).toContain('<font');
    expect(container.querySelector('script,img,font,[onclick]')).toBeNull();
    expect(container.querySelector('a')?.getAttribute('href')).not.toContain('javascript:');
  });
});
