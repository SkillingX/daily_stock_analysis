import apiClient from './index';
import { API_BASE_URL } from '../utils/constants';
import { createApiError, isApiRequestError, parseApiError } from './error';
import { downloadPdfFromUrl } from './download';

export interface DeepResearchGenerateRequest {
  stock_code: string;
  stock_name?: string;
  report_type?: string;
  /** 维度子集（省钱模式）：不传/空 = 全部 11 维度；传入选中维度 id（后端自动补依赖闭包） */
  dims?: string[];
  /** 跳过维度缓存强制重算 */
  force_refresh?: boolean;
}

export interface DeepResearchStreamOptions {
  signal?: AbortSignal;
}

export interface DeepResearchReportItem {
  id: string;
  stock_code: string;
  stock_name?: string;
  created_at?: string;
  status?: string;
  quality_score?: number;
  missing_layers?: string[];
  has_pdf?: boolean;
}

export interface DeepResearchReportDetail extends DeepResearchReportItem {
  markdown: string;
  md_path?: string;
  total_steps?: number;
  total_tokens?: number;
  provider?: string;
  error?: string;
  engine?: 'legacy' | 'dual_track';
}

/** 双轨引擎 done 事件的护栏事件（规则 id / 维度 / 处置 / 理由） */
export interface DeepResearchGuardrailEvent {
  rule_id: string;
  dim: string;
  action: string;
  reason: string;
}

/**
 * A股深度投研报告 API 客户端。
 *
 * 路径前缀 `/api/v1/deep-research/...`（继承全局 AuthMiddleware）。
 * 与 chat 类 API 的差异：generate 是 SSE 流式一次性生成（非多轮对话），
 * reports 是历史报告 CRUD（元数据在 SQLite，正文/PDF 在文件）。
 */
export const deepResearchApi = {
  async getReports(limit = 50, offset = 0): Promise<DeepResearchReportItem[]> {
    const response = await apiClient.get<{
      success: boolean;
      data: DeepResearchReportItem[];
      total: number;
    }>('/api/v1/deep-research/reports', { params: { limit, offset } });
    return response.data.data;
  },

  async getReport(reportId: string): Promise<DeepResearchReportDetail> {
    const response = await apiClient.get<{
      success: boolean;
      data: DeepResearchReportDetail;
    }>(`/api/v1/deep-research/reports/${reportId}`);
    return response.data.data;
  },

  async deleteReport(reportId: string): Promise<void> {
    await apiClient.delete(`/api/v1/deep-research/reports/${reportId}`);
  },

  /**
   * SSE 流式生成深度投研报告。
   * 返回原始 Response，由 useDeepResearch hook 解析 `data: ` 事件流。
   * 事件类型：thinking / tool_start / tool_done / generating / done / error / heartbeat。
   */
  async generateStream(
    payload: DeepResearchGenerateRequest,
    options?: DeepResearchStreamOptions,
  ): Promise<Response> {
    const base = API_BASE_URL || '';
    const url = `${base}/api/v1/deep-research/generate/stream`;
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: options?.signal,
      });

      if (response.ok) {
        return response;
      }

      const contentType = response.headers.get('content-type') || '';
      let responseData: unknown = null;
      if (contentType.includes('application/json')) {
        responseData = await response.json().catch(() => null);
      } else {
        responseData = await response.text().catch(() => null);
      }

      const parsed = parseApiError({
        response: {
          status: response.status,
          statusText: response.statusText,
          data: responseData,
        },
      });
      throw createApiError(parsed, {
        response: {
          status: response.status,
          statusText: response.statusText,
          data: responseData,
        },
      });
    } catch (error: unknown) {
      if (isApiRequestError(error)) {
        throw error;
      }
      if (error instanceof Error && error.name === 'AbortError') {
        throw error;
      }

      const parsed = parseApiError(error);
      throw createApiError(parsed, { cause: error });
    }
  },

  /**
   * 下载报告 PDF（惰性生成：首次请求触发后端 xhtml2pdf 渲染）。
   * 用 fetch blob + <a download> 触发浏览器下载（带认证 cookie，不被弹窗拦截）。
   */
  async downloadPdf(reportId: string): Promise<void> {
    const base = API_BASE_URL || '';
    const url = `${base}/api/v1/deep-research/reports/${reportId}/pdf`;
    await downloadPdfFromUrl(url, 'deep_research', reportId);
  },

  /**
   * 下载报告 Markdown 原文件（双轨/legacy 通用）。
   */
  async downloadMarkdown(reportId: string): Promise<void> {
    const base = API_BASE_URL || '';
    const url = `${base}/api/v1/deep-research/reports/${reportId}/markdown`;
    await downloadPdfFromUrl(url, 'deep_research', reportId);
  },

  /**
   * 双轨引擎维度 JSON 产物（11 维度结构化数据 + 护栏事件表）。
   * legacy 报告无产物，返回 null。
   */
  async getDims(reportId: string): Promise<{ guardrail_events: DeepResearchGuardrailEvent[]; dimensions: Record<string, unknown> } | null> {
    try {
      const response = await apiClient.get<{ success: boolean; data: { guardrail_events: DeepResearchGuardrailEvent[]; dimensions: Record<string, unknown> } }>(
        `/api/v1/deep-research/reports/${reportId}/dims`,
      );
      return response.data.data;
    } catch {
      return null;
    }
  },
};
