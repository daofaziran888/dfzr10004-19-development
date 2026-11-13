<template>
  <section class="page" data-module="audit">
    <header class="page-head">
      <div>
        <h2>采集审计日志管理</h2>
        <p class="page-desc">维护采集审计的操作类型、执行记录与复核状态；保留策略试算通过后，受影响记录进入待归档状态并在此同步展示。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记审计记录</button>
        <label class="btn">
          导入审计清单
          <input type="file" accept="application/json" class="file-input" @change="importList" />
        </label>
        <button class="btn" type="button" @click="exportRows">导出采集审计日志清单</button>
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
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters['status']">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td><span class="badge" :class="`badge-${row.status}`">{{ row.status }}</span></td>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集审计日志数据，可先登记审计记录</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <h3>待归档清单（由保留策略试算同步）</h3>
      <p class="panel-desc">试算通过后生成的待归档清单在此接收；记录归档后从清单中移除，历史已归档记录仍可在上方查询。</p>
      <div v-for="list in pendingLists" :key="list['清单编号']" class="pending-list">
        <p class="panel-desc">
          清单 {{ list['清单编号'] }} · 试算 {{ list['试算编号'] }} · 配置 {{ list['配置编号'] }} · 生成于 {{ list['生成时间'] }}
        </p>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>原状态</th><th>执行时间</th><th>到期日</th></tr></thead>
          <tbody>
            <tr v-for="item in list['记录']" :key="item['记录编号']">
              <td>{{ item['记录编号'] }}</td>
              <td>{{ item['原状态'] }}</td>
              <td>{{ item['执行时间'] }}</td>
              <td>{{ item['到期日'] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!pendingLists.length" class="empty-state">暂无待归档清单，可先在采集存储策略页完成保留策略试算</p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集审计日志记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="ok-text">{{ infoMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/audit'
const columns = ["记录编号", "操作类型", "采集计划", "执行人", "执行时间", "结果摘要", "变更内容", "复核状态"]
const actions = ["标记复核", "归档记录"]
const statuses = ["已记录", "已复核", "待归档", "已归档"]
const stats = ref([
  { label: '审计记录总数', value: 0 },
  { label: '已复核记录', value: 0 },
  { label: '待归档记录', value: 0 },
  { label: '已归档记录', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const pendingLists = ref<Row[]>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '审计记录登记入口尚未接入审批流'
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
      throw new Error(payload.message ?? payload.detail ?? '采集审计日志动作未生效，请稍后重试')
    }
    infoMessage.value = payload.message
    await Promise.all([reload(), reloadPendingLists()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志操作失败'
  }
}

async function importList(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const doc = JSON.parse(await file.text())
    const items = Array.isArray(doc) ? doc : doc.items
    const response = await request(`${ENDPOINT}/import`, {
      method: 'POST',
      body: JSON.stringify({ values: { items } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message ?? payload.detail ?? '审计清单导入失败')
    }
    infoMessage.value = payload.skipped?.length
      ? `${payload.message}（跳过：${payload.skipped.join('、')}）`
      : payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审计清单导入失败'
  } finally {
    input.value = ''
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('审计记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => row.status === '已复核').length
    stats.value[2].value = rows.value.filter((row) => row.status === '待归档').length
    stats.value[3].value = rows.value.filter((row) => row.status === '已归档').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集审计日志列表读取失败'
  }
}

async function reloadPendingLists() {
  try {
    const response = await request('/api/retention/pending-list')
    const payload = await response.json()
    pendingLists.value = payload.items ?? []
  } catch {
    pendingLists.value = []
  }
}

onMounted(() => {
  void reload()
  void reloadPendingLists()
})
</script>
