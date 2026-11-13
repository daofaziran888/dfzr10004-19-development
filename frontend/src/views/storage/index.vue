<template>
  <section class="page" data-module="storage">
    <header class="page-head">
      <div>
        <h2>采集存储策略管理</h2>
        <p class="page-desc">维护采集存储的路径规划、格式选择与归档策略；启用前须通过保留策略试算，容量估算、待归档清单与启用资格以最新试算为准。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记存储配置</button>
        <button class="btn" type="button" @click="policyPanelVisible = !policyPanelVisible">粘贴导入策略包</button>
        <label class="btn">
          上传试算结果
          <input type="file" accept="application/json" class="file-input" @change="uploadTrial" />
        </label>
        <button class="btn" type="button" @click="exportRows">导出采集存储策略清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openTrial(row)">保留试算</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集存储策略数据，可先登记存储配置</td>
        </tr>
      </tbody>
    </table>

    <section v-if="policyPanelVisible" class="panel">
      <h3>粘贴导入保留策略包</h3>
      <p class="panel-desc">粘贴 JSON 格式的保留策略包（配置编号、存储路径、文件格式、分片大小、压缩算法、保留天数、归档策略）。配置已存在时先展示差异，确认后才落库。</p>
      <textarea v-model="policyText" class="paste-area" rows="6" placeholder='{"配置编号":"STOR-0002","存储路径":"/data/collect/audit","文件格式":"JSON","分片大小":"256MB","压缩算法":"zstd","保留天数":"90","归档策略":"到期归档"}'></textarea>
      <div class="panel-actions">
        <button class="btn primary" type="button" @click="importPolicy(false)">导入策略包</button>
        <button class="btn ghost" type="button" @click="resetPolicyPanel">清空</button>
      </div>
      <div v-if="policyDiff.length" class="diff-box">
        <p class="warn-text">配置已存在，{{ policyDiff.length }} 个字段有差异，确认后才会覆盖：</p>
        <table class="data-table">
          <thead><tr><th>字段</th><th>原值</th><th>新值</th></tr></thead>
          <tbody>
            <tr v-for="item in policyDiff" :key="item.字段">
              <td>{{ item.字段 }}</td><td>{{ item.原值 ?? '—' }}</td><td>{{ item.新值 ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="panel-actions">
          <button class="btn primary" type="button" @click="importPolicy(true)">确认导入</button>
          <button class="btn ghost" type="button" @click="resetPolicyPanel">取消</button>
        </div>
      </div>
    </section>

    <section v-if="trialConfig" class="panel">
      <h3>保留策略试算：{{ trialConfig['配置编号'] }}</h3>
      <p class="panel-desc">按存储路径、文件格式、分片大小、保留天数与归档策略，估算未来一段时间的分片数、预计容量与过期区间，并列出受影响的采集审计记录。</p>
      <form class="filter-bar" @submit.prevent="submitTrial">
        <label class="filter-item">
          <span>试算天数</span>
          <input v-model.number="trialForm.试算天数" type="number" min="1" max="365" />
        </label>
        <label class="filter-item">
          <span>日增数据量(MB)</span>
          <input v-model.number="trialForm.日增数据量MB" type="number" min="1" />
        </label>
        <button class="btn primary" type="submit">提交试算</button>
        <button class="btn ghost" type="button" @click="trialConfig = null">收起</button>
      </form>

      <div v-if="trialResult" class="trial-result">
        <div class="stat-row">
          <article class="stat-card"><span class="stat-label">试算编号</span><strong class="stat-value">{{ trialResult['试算编号'] }}</strong></article>
          <article class="stat-card"><span class="stat-label">分片数</span><strong class="stat-value">{{ trialResult['分片数'] }}</strong></article>
          <article class="stat-card"><span class="stat-label">预计容量</span><strong class="stat-value">{{ trialResult['预计容量MB'] }} MB</strong></article>
          <article class="stat-card"><span class="stat-label">过期区间</span><strong class="stat-value stat-date">{{ trialResult['过期区间'][0] }} ~ {{ trialResult['过期区间'][1] }}</strong></article>
          <article class="stat-card">
            <span class="stat-label">启用资格</span>
            <strong class="stat-value" :class="trialResult['可启用'] ? 'ok-text' : 'error-text'">{{ trialResult['可启用'] ? '可启用' : '不得启用' }}</strong>
          </article>
        </div>
        <p class="panel-desc">
          状态：{{ trialResult.status }} · 压缩率 {{ trialResult['压缩率'] }} · 格式系数 {{ trialResult['格式系数'] }} ·
          原始容量 {{ trialResult['原始容量MB'] }} MB · 归档窗口 {{ trialResult['归档窗口'][0] }} ~ {{ trialResult['归档窗口'][1] }}
        </p>
        <p v-if="trialResult['阻断记录']?.length" class="error-text">
          试算会删除仍被待复核记录引用的数据：{{ trialResult['阻断记录'].map((item: any) => item['记录编号']).join('、') }}，该存储配置不得启用。
        </p>
        <h4>受影响的采集审计记录</h4>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>当前状态</th><th>执行时间</th><th>到期日</th></tr></thead>
          <tbody>
            <tr v-for="item in trialResult['受影响记录']" :key="item['记录编号']">
              <td>{{ item['记录编号'] }}</td><td>{{ item['当前状态'] }}</td><td>{{ item['执行时间'] }}</td><td>{{ item['到期日'] }}</td>
            </tr>
            <tr v-if="!trialResult['受影响记录']?.length"><td colspan="4" class="empty-state">试算窗口内没有受影响的审计记录</td></tr>
          </tbody>
        </table>
        <h4>待归档清单（{{ trialResult['待归档清单']?.['清单编号'] }}）</h4>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>原状态</th><th>到期日</th></tr></thead>
          <tbody>
            <tr v-for="item in trialResult['待归档清单']?.['记录'] ?? []" :key="item['记录编号']">
              <td>{{ item['记录编号'] }}</td><td>{{ item['原状态'] }}</td><td>{{ item['到期日'] }}</td>
            </tr>
            <tr v-if="!trialResult['待归档清单']?.['记录']?.length"><td colspan="3" class="empty-state">暂无待归档记录</td></tr>
          </tbody>
        </table>
        <ul v-if="trialResult['估算说明']?.length" class="note-list">
          <li v-for="(note, index) in trialResult['估算说明']" :key="index">{{ note }}</li>
        </ul>
      </div>
    </section>

    <section class="panel">
      <h3>最新试算结果（同一配置仅保留最新）</h3>
      <table class="data-table">
        <thead>
          <tr><th>试算编号</th><th>配置编号</th><th>状态</th><th>分片数</th><th>预计容量(MB)</th><th>过期区间</th><th>可启用</th><th>来源</th><th>可执行动作</th></tr>
        </thead>
        <tbody>
          <tr v-for="trial in trials" :key="trial['试算编号']">
            <td>{{ trial['试算编号'] }}</td>
            <td>{{ trial['配置编号'] }}</td>
            <td>{{ trial.status }}</td>
            <td>{{ trial['分片数'] }}</td>
            <td>{{ trial['预计容量MB'] }}</td>
            <td>{{ trial['过期区间'][0] }} ~ {{ trial['过期区间'][1] }}</td>
            <td :class="trial['可启用'] ? 'ok-text' : 'error-text'">{{ trial['可启用'] ? '是' : '否' }}</td>
            <td>{{ trial['来源'] }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="runTrialAction(trial, '确认试算')">确认试算</button>
              <button class="link" type="button" @click="runTrialAction(trial, '归档结果')">归档结果</button>
              <button class="link" type="button" @click="exportTrial(trial)">导出结果</button>
            </td>
          </tr>
          <tr v-if="!trials.length"><td colspan="9" class="empty-state">暂无试算结果，可在上方对存储配置发起保留试算</td></tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集存储策略记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="ok-text">{{ infoMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/storage'
const RETENTION_ENDPOINT = '/api/retention'
const columns = ["配置编号", "存储路径", "文件格式", "分片大小", "压缩算法", "保留天数", "归档策略", "是否启用"]
const actions = ["提交校验", "启用配置", "停用配置"]
const stats = [{ "label": "存储配置总数", "value": 0 }, { "label": "待校验配置", "value": 0 }, { "label": "已启用配置", "value": 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const trials = ref<Row[]>([])
const trialConfig = ref<Row | null>(null)
const trialResult = ref<Row | null>(null)
const trialForm = ref({ 试算天数: 30, 日增数据量MB: 512 })

const policyPanelVisible = ref(false)
const policyText = ref('')
const policyDiff = ref<Row[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '存储配置登记入口尚未接入审批流'
}

function openTrial(row: Row) {
  trialConfig.value = row
  trialResult.value = null
  errorMessage.value = ''
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '采集存储策略动作未生效，请稍后重试')
    }
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集存储策略操作失败'
  }
}

async function submitTrial() {
  if (!trialConfig.value) return
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${RETENTION_ENDPOINT}/trials`, {
      method: 'POST',
      body: JSON.stringify({ config_id: trialConfig.value.id, ...trialForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '试算未登记')
    }
    trialResult.value = payload.entry
    infoMessage.value = payload.message
    await Promise.all([reloadTrials(), reload()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '保留策略试算失败'
  }
}

async function runTrialAction(trial: Row, action: string) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${RETENTION_ENDPOINT}/trials/${trial.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '试算动作未生效')
    }
    infoMessage.value = payload.message
    await reloadTrials()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试算动作失败'
  }
}

async function exportTrial(trial: Row) {
  const response = await request(`${RETENTION_ENDPOINT}/trials/${trial.id}/export`)
  const payload = await response.json()
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = `${trial['试算编号']}.json`
  link.click()
  URL.revokeObjectURL(link.href)
}

async function uploadTrial(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const doc = JSON.parse(await file.text())
    const response = await request(`${RETENTION_ENDPOINT}/import`, {
      method: 'POST',
      body: JSON.stringify({ values: doc }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '试算结果导入失败')
    }
    infoMessage.value = payload.message
    await reloadTrials()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试算结果导入失败'
  } finally {
    input.value = ''
  }
}

async function importPolicy(confirm: boolean) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const values = JSON.parse(policyText.value)
    const response = await request(`${ENDPOINT}/import-policy`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...values, confirm } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '策略包导入失败')
    }
    if (payload.needs_confirm) {
      policyDiff.value = payload.diff ?? []
      infoMessage.value = payload.message
      return
    }
    policyDiff.value = []
    policyText.value = ''
    infoMessage.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '策略包导入失败'
  }
}

function resetPolicyPanel() {
  policyText.value = ''
  policyDiff.value = []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('存储配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = total.value
    stats[1].value = rows.value.filter((row) => row.status === '待校验').length
    stats[2].value = rows.value.filter((row) => row.status === '已启用').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集存储策略列表读取失败'
  }
}

async function reloadTrials() {
  try {
    const response = await request(`${RETENTION_ENDPOINT}/trials`)
    const payload = await response.json()
    trials.value = payload.items ?? []
  } catch {
    trials.value = []
  }
}

onMounted(() => {
  void reload()
  void reloadTrials()
})
</script>
