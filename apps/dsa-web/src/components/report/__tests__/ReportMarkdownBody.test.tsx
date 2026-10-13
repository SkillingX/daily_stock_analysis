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

it('shows legacy disclosure content without raw details tags and keeps code literal', () => {
  const { container } = render(<ReportMarkdownBody content={'<details>\n<summary>未来事件与预案</summary>\n\n- **公告**：等待披露\n\n</details>\n\n```html\n<details><summary>example</summary></details>\n```'} />);
  expect(container.querySelector('li')?.textContent).toContain('公告');
  expect(container.querySelector('code')?.textContent).toContain('<details>');
  expect(container.querySelector('details,summary')).toBeNull();
  expect(container.textContent?.replace(container.querySelector('code')?.textContent || '', '')).not.toContain('<summary>');
});

it('hides report split markers while preserving code examples', () => {
  const { container } = render(<ReportMarkdownBody content={'<!-- dim:ownership -->\n\n## 股权资料\n\n```html\n<!-- dim:ownership -->\n```'} />);
  const code = container.querySelector('code')?.textContent || '';
  expect(code).toContain('<!-- dim:ownership -->');
  expect(container.textContent?.replace(code, '')).not.toContain('<!-- dim:');
});
