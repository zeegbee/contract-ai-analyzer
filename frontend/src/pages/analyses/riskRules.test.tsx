import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { makeMe, serveMe } from '../../test/auth'
import {
  analysisHandlers,
  availableModels,
  localModel,
  makeAnalysis,
  makeDocument,
  makeRisk,
} from '../../test/fixtures'
import { renderRoute } from '../../test/render'
import { availableRulesHandler, makeRule } from '../../test/riskRules'
import { server } from '../../test/server'

const PENALTY = makeRule()
const NOT_DEFAULT = makeRule({ id: 'rr-2', code: 'RR-002', title: 'Штраф без лимита', default_selected: false })
const ACCEPTANCE = makeRule({
  id: 'rr-3',
  code: 'RR-003',
  title: 'Нет приёмки по акту',
  category: 'acceptance',
  kind: 'missing_clause',
  description: 'В договоре нет порядка приёмки услуг по акту',
})

function serveForm(limit = 20) {
  const doc = makeDocument()
  const bodies: Record<string, unknown>[] = []
  server.use(
    http.get('*/api/documents', () => HttpResponse.json({ items: [doc], next_cursor: null })),
    http.get('*/api/documents/d-1', () => HttpResponse.json(doc)),
    http.get('*/api/models/available', () => availableModels([localModel])),
    availableRulesHandler([PENALTY, NOT_DEFAULT, ACCEPTANCE], { per_analysis: limit }),
    http.post('*/api/analyses/estimate', async ({ request }) => {
      bodies.push((await request.json()) as Record<string, unknown>)
      return HttpResponse.json({
        chunks: 2,
        estimated_input_tokens: 12000,
        estimated_output_tokens: 8000,
        estimated_tokens: 20000,
        token_limit: 1500000,
        within_limit: true,
        estimated_cost: null,
        cost_currency: null,
        pricing_source: null,
        cloud_transfer: false,
        rules_extra_tokens: 6000,
        note: 'Оценка по длине текста',
      })
    }),
    http.post('*/api/analyses', async ({ request }) => {
      bodies.push((await request.json()) as Record<string, unknown>)
      return HttpResponse.json(makeAnalysis({ status: 'queued' }), { status: 201 })
    }),
    ...analysisHandlers(makeAnalysis({ status: 'queued', summary: null, coverage: null })),
  )
  return bodies
}

describe('правила организации в форме анализа', () => {
  it('правила вложены в категории, снимаются вместе с ними и передаются при запуске', async () => {
    serveMe(makeMe('analyst'))
    const bodies = serveForm()
    renderRoute('/analyses/new?document=d-1')
    const user = userEvent.setup()
    const penalties = await screen.findByRole('list', { name: 'Правила организации: Штрафы, пени, неустойки' })
    const rule1 = within(penalties).getByRole('checkbox', { name: /RR-001 Высокая неустойка/ })
    const rule2 = within(penalties).getByRole('checkbox', { name: /RR-002 Штраф без лимита/ })
    const rule3 = screen.getByRole('checkbox', { name: /RR-003 Нет приёмки по акту/ })
    // По умолчанию отмечены правила с «отмечено по умолчанию».
    expect(rule1).toBeChecked()
    expect(rule2).not.toBeChecked()
    expect(rule3).toBeChecked()
    expect(screen.getByText(/Правил выбрано: 2 из 20/)).toBeInTheDocument()
    expect(rule3.closest('label')).toHaveTextContent('отсутствующее условие · В договоре нет порядка приёмки')

    // Снятие категории снимает и блокирует её правила; возврат категории восстанавливает выбор.
    await user.click(screen.getByRole('checkbox', { name: 'Приёмка' }))
    expect(rule3).not.toBeChecked()
    expect(rule3).toBeDisabled()
    expect(screen.getByText(/Правил выбрано: 1 из 20/)).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Приёмка' }))
    expect(rule3).toBeChecked()

    await user.click(rule2)
    await user.click(rule1)
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Модель' }), 'm-local')
    await user.click(screen.getByRole('button', { name: 'Проверить модель и объём' }))
    expect(await screen.findByText(/Из них на правила организации: 6/)).toBeInTheDocument()
    expect(screen.getByText(/Правила заметно увеличивают объём анализа/)).toBeInTheDocument()
    expect(bodies[0]?.rule_ids).toEqual(['rr-2', 'rr-3'])

    await user.click(screen.getByRole('button', { name: 'Запустить анализ' }))
    await waitFor(() => expect(bodies).toHaveLength(2))
    expect(bodies[1]?.rule_ids).toEqual(['rr-2', 'rr-3'])
  })

  it('«Снять все» и «Выбрать все» действуют на правила; лимит блокирует запуск', async () => {
    serveMe(makeMe('analyst'))
    const bodies = serveForm(2)
    renderRoute('/analyses/new?document=d-1')
    const user = userEvent.setup()
    const rule2 = await screen.findByRole('checkbox', { name: /RR-002/ })

    await user.click(screen.getByRole('button', { name: 'Снять все' }))
    expect(screen.getByText(/Правил выбрано: 0 из 2/)).toBeInTheDocument()
    expect(rule2).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: 'Штрафы, пени, неустойки' }))
    // После «Снять все» правила категории не отмечаются заново сами.
    expect(rule2).not.toBeChecked()

    await user.click(screen.getByRole('button', { name: 'Выбрать все' }))
    expect(rule2).toBeChecked()
    expect(screen.getByText(/Правил выбрано: 3 из 2/)).toHaveTextContent('Снимите лишние')
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Модель' }), 'm-local')
    expect(screen.getByRole('button', { name: 'Запустить анализ' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Проверить модель и объём' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Выбрано больше правил')
    expect(bodies).toEqual([])
  })

  it('без правил форма работает как прежде', async () => {
    serveMe(makeMe('analyst'))
    const bodies = serveForm()
    server.use(availableRulesHandler([]))
    renderRoute('/analyses/new?document=d-1')
    const user = userEvent.setup()
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Модель' }), 'm-local')
    expect(screen.queryByText(/Правил выбрано/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Запустить анализ' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).not.toHaveProperty('rule_ids')
  })
})

describe('правила организации в отчёте', () => {
  it('бейдж правила, фильтр по правилу и список применённых правил', async () => {
    serveMe(makeMe('analyst'))
    const base = makeAnalysis()
    const summary = base.summary
    if (!summary) throw new Error('fixture')
    const analysis = makeAnalysis({
      summary: {
        ...summary,
        risk_rules: [
          { code: 'RR-001', title: 'Высокая неустойка', category: 'penalties', kind: 'clause_risk', revision: 2, risks: 1 },
          { code: 'RR-003', title: 'Нет приёмки по акту', category: 'acceptance', kind: 'missing_clause', revision: 1, risks: 0 },
        ],
      },
    })
    const risks = [
      makeRisk({ id: 'r-1', risk_key: 'R-001', title: 'Неустойка 1 % в день', category: 'penalties', rule_code: 'RR-001' }),
      makeRisk({
        id: 'r-2',
        risk_key: 'R-002',
        title: 'Одностороннее изменение цены',
        rule_code: null,
        validation_notes: { rule_code_unknown: true, status_reasons: [] },
      }),
    ]
    server.use(...analysisHandlers(analysis, risks))
    renderRoute('/analyses/a-1')
    const user = userEvent.setup()
    const card = (await screen.findByRole('heading', { name: 'Неустойка 1 % в день' })).closest('article')
    if (!card) throw new Error('card')
    expect(within(card).getByText('Правило RR-001 · Высокая неустойка')).toBeInTheDocument()
    expect(screen.getByText(/Модель сослалась на правило, которого нет в анализе/)).toBeInTheDocument()

    await user.click(screen.getByText('Применены правила организации: 2'))
    expect(screen.getByText(/Нет приёмки по акту \(Приёмка; ред\. 1\) — рисков: 0/)).toBeInTheDocument()

    const filter = screen.getByLabelText('Правило организации')
    await user.selectOptions(filter, 'RR-001')
    expect(screen.queryByRole('heading', { name: 'Одностороннее изменение цены' })).not.toBeInTheDocument()
    expect(screen.getByText(/Показано: 1 \/ 2/)).toBeInTheDocument()
    await user.selectOptions(filter, '__none__')
    expect(screen.getByRole('heading', { name: 'Одностороннее изменение цены' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Неустойка 1 % в день' })).not.toBeInTheDocument()
  })

  it('отчёт анализа без правил не показывает фильтр правил', async () => {
    serveMe(makeMe('analyst'))
    server.use(...analysisHandlers(makeAnalysis()))
    renderRoute('/analyses/a-1')
    expect(await screen.findByRole('heading', { name: 'Риски' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Правило организации')).not.toBeInTheDocument()
    expect(screen.queryByText(/Применены правила организации/)).not.toBeInTheDocument()
  })
})
