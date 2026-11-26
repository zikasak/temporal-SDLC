import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { TopLevelFlow } from '@part/src/Flows';

type Props = Parameters<typeof TopLevelFlow>[0];
type FlowProp = Props['flow'];

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mounted: { root: Root; host: HTMLElement }[] = [];

function flowOf(overrides: Partial<FlowProp>): FlowProp {
  return {
    workflow_id: 'req-1',
    run_id: 'run-1',
    workflow_type: 'RequirementsWorkflow',
    status: 'running',
    parent_workflow_id: null,
    start_time: '2026-11-01T10:00:00Z',
    close_time: null,
    repository: null,
    branch: 'main',
    name: null,
    origin: null,
    linked: false,
    // A flow from before the backend reported its flags: they are read from its session.
    waiting_for_user: null,
    unresolved_findings: null,
    ...overrides,
  };
}

function deliveryProgress(openFindings: boolean): Record<string, unknown> {
  return {
    publication: {
      branch: 'delivery/req-1',
      commit: '0123456789abcdef',
      url: 'https://github.com/acme/widgets/tree/delivery/req-1',
      ignored: [],
      ignored_count: 0,
      verified: !openFindings,
    },
    publication_status: 'completed',
    pull_request: {
      number: 7,
      title: 'Add the widget endpoint',
      url: 'https://github.com/acme/widgets/pull/7',
      draft: openFindings,
      base: 'main',
    },
    pull_request_status: 'completed',
    agent_questions: [],
    reviews: [],
    mentions: [],
    open_findings: openFindings,
    findings_acceptance: null,
  };
}

function requirementsSession(openFindings: boolean): Record<string, unknown> {
  return {
    messages: [
      { role: 'user', content: 'I need a widget service.' },
      { role: 'assistant', content: 'Recorded.' },
    ],
    requirements: [],
    kind: 'bugfix',
    status: 'completed',
    document: { document_id: 'doc-1' },
    error: null,
    delivery: deliveryProgress(openFindings),
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Fakes fetch: the flow's session is `session`, its child list is empty. */
function stubFetch(sessionPath: string, session: unknown): string[] {
  return stubFetchSessions([[sessionPath, session]]);
}

/** Fakes fetch for several sessions; every child list is empty (WorkflowPage: `workflows`). */
function stubFetchSessions(sessions: [string, unknown][]): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      urls.push(url);
      for (const [sessionPath, session] of sessions) {
        if (url.includes(sessionPath)) return jsonResponse(session);
      }
      if (url.includes('/workflows')) return jsonResponse({ workflows: [], next_page_token: null });
      return jsonResponse({ detail: 'not found' }, 404);
    }),
  );
  return urls;
}

async function renderFlow(flow: FlowProp, onOpen: (link: string) => void): Promise<HTMLElement> {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => {
    root.render(
      createElement(
        'ul',
        null,
        createElement(TopLevelFlow, { flow, tick: 0, onOpen, onFilter: vi.fn() }),
      ),
    );
  });
  return host;
}

function openQuestionsButton(host: HTMLElement): HTMLButtonElement | undefined {
  return Array.from(host.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes('Open questions'),
  );
}

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 100));
  });
}

beforeEach(() => {
  vi.useRealTimers();
});

afterEach(async () => {
  for (const { root, host } of mounted.splice(0)) {
    await act(async () => root.unmount());
    host.remove();
  }
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('TopLevelFlow with open review findings', () => {
  test('highlights a running flow with an Open questions button while a closed flow with the same findings only gets the marker', async () => {
    const urls = stubFetchSessions([
      ['/api/requirements/sessions/req-1', requirementsSession(true)],
      ['/api/requirements/sessions/req-2', requirementsSession(true)],
    ]);
    const onOpenRunning = vi.fn();
    const onOpenClosed = vi.fn();

    const running = await renderFlow(flowOf({ status: 'running' }), onOpenRunning);
    const closed = await renderFlow(
      flowOf({ workflow_id: 'req-2', status: 'completed', close_time: '2026-11-02T10:00:00Z' }),
      onOpenClosed,
    );

    await vi.waitFor(() => expect(openQuestionsButton(running)).toBeDefined());
    await vi.waitFor(() => expect(closed.textContent?.toLowerCase()).toContain('unresolved findings'));
    expect(urls.some((u) => u.includes('/api/requirements/sessions/req-1'))).toBe(true);
    expect(urls.some((u) => u.includes('/api/requirements/sessions/req-2'))).toBe(true);
    expect(running.textContent?.toLowerCase()).not.toContain('unresolved findings');
    expect(openQuestionsButton(closed)).toBeUndefined();

    await act(async () => {
      (openQuestionsButton(running) as HTMLButtonElement).click();
    });
    expect(onOpenRunning).toHaveBeenCalledTimes(1);
    expect(typeof onOpenRunning.mock.calls[0][0]).toBe('string');
    expect(onOpenRunning.mock.calls[0][0]).not.toBe('');
    expect(onOpenClosed).not.toHaveBeenCalled();
  });

  test('shows a completed flow with an unresolved findings marker and without an Open questions button', async () => {
    stubFetch('/api/requirements/sessions/req-1', requirementsSession(true));
    const onOpen = vi.fn();

    const host = await renderFlow(flowOf({ status: 'completed', close_time: '2026-11-02T10:00:00Z' }), onOpen);

    await vi.waitFor(() => expect(host.textContent?.toLowerCase()).toContain('unresolved findings'));
    expect(openQuestionsButton(host)).toBeUndefined();
    expect(host.textContent).not.toContain('Review findings wait for your decision');
    expect(onOpen).not.toHaveBeenCalled();
  });

  test('shows a canceled flow with an unresolved findings marker and without an Open questions button', async () => {
    stubFetch('/api/requirements/sessions/req-1', requirementsSession(true));
    const onOpen = vi.fn();

    const host = await renderFlow(flowOf({ status: 'canceled', close_time: '2026-11-02T10:00:00Z' }), onOpen);

    await vi.waitFor(() => expect(host.textContent?.toLowerCase()).toContain('unresolved findings'));
    expect(openQuestionsButton(host)).toBeUndefined();
    expect(host.textContent).not.toContain('Review findings wait for your decision');
  });

  test('shows the marker on a closed onboarding too, read from the onboarding session', async () => {
    stubFetch('/api/onboarding/sessions/onb-1', {
      messages: [],
      status: 'completed',
      error: null,
      delivery: deliveryProgress(true),
    });

    const host = await renderFlow(
      flowOf({
        workflow_id: 'onb-1',
        workflow_type: 'OnboardingWorkflow',
        status: 'completed',
        close_time: '2026-11-02T10:00:00Z',
      }),
      vi.fn(),
    );

    await vi.waitFor(() => expect(host.textContent?.toLowerCase()).toContain('unresolved findings'));
    expect(openQuestionsButton(host)).toBeUndefined();
  });

  test('marks a closed flow only while its findings are open', async () => {
    stubFetch('/api/requirements/sessions/req-1', requirementsSession(true));
    const withFindings = await renderFlow(flowOf({ status: 'completed', close_time: '2026-11-02T10:00:00Z' }), vi.fn());
    await vi.waitFor(() => expect(withFindings.textContent?.toLowerCase()).toContain('unresolved findings'));

    vi.unstubAllGlobals();
    const urls = stubFetch('/api/requirements/sessions/req-2', requirementsSession(false));
    const withoutFindings = await renderFlow(
      flowOf({ workflow_id: 'req-2', status: 'completed', close_time: '2026-11-02T10:00:00Z' }),
      vi.fn(),
    );
    await vi.waitFor(() => expect(urls.some((u) => u.includes('/api/requirements/sessions/req-2'))).toBe(true));
    await settle();
    expect(withoutFindings.textContent?.toLowerCase()).not.toContain('unresolved findings');
    expect(openQuestionsButton(withoutFindings)).toBeUndefined();
  });
});
