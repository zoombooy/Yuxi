<template>
  <transition name="governance-slide-fade">
    <div v-if="visible" class="floating-governance-panel">
      <div class="panel-header">
        <span class="panel-title">知识治理</span>
        <a-button
          size="small"
          type="text"
          :disabled="factsLoading"
          class="panel-refresh-btn"
          @click="reload"
        >
          <RefreshCw :size="14" :class="{ spin: factsLoading }" />
        </a-button>
      </div>
      <div class="panel-body">
        <div v-if="!readonly" class="config-block">
          <div class="config-row">
            <span class="config-label">治理开关</span>
            <a-switch v-model:checked="configForm.enabled" size="small" />
          </div>
          <div class="config-row">
            <span class="config-label">自动批准阈值</span>
            <a-input-number
              v-model:value="configForm.auto_approve_threshold"
              :min="0"
              :max="1"
              :step="0.05"
              size="small"
              style="width: 90px"
            />
          </div>
          <a-button
            size="small"
            type="primary"
            block
            :loading="configSaving"
            class="config-save-btn"
            @click="saveConfig"
          >
            保存治理配置
          </a-button>
          <div class="config-hint">开启后，新抽取的关系先进入待审队列，仅已批准关系写入图谱。</div>
        </div>

        <div class="as-of-block">
          <div class="config-row">
            <span class="config-label">时点查询</span>
          </div>
          <div class="as-of-row">
            <a-input
              v-model:value="asOfInput"
              size="small"
              placeholder="如 2024-03-15"
              @keydown.enter="runAsOfQuery"
            />
            <a-button size="small" :loading="factsLoading" @click="runAsOfQuery">查询</a-button>
            <a-button v-if="asOfActive" size="small" type="text" @click="clearAsOf">清除</a-button>
          </div>
          <div v-if="asOfActive" class="as-of-banner">
            以下为 {{ asOfActive }} 时点成立的事实（世界轴判定）
          </div>
        </div>

        <div class="counts-row">
          <div
            v-for="tab in statusTabs"
            :key="tab.key"
            class="count-chip"
            :class="{ active: !asOfActive && activeStatus === tab.key }"
            role="button"
            :tabindex="0"
            @click="switchStatus(tab.key)"
            @keydown.enter.prevent="switchStatus(tab.key)"
          >
            <span class="count-value">{{ statusCounts[tab.key] ?? 0 }}</span>
            <span class="count-label">{{ tab.label }}</span>
          </div>
        </div>

        <div class="facts-list">
          <div v-if="factsLoading" class="facts-loading">
            <Loader2 :size="16" class="spin" />
            加载中…
          </div>
          <a-empty
            v-else-if="!facts.length"
            :description="asOfActive ? '该时点没有成立的事实' : '暂无事实'"
          />
          <div v-else>
            <div v-for="fact in facts" :key="fact.fact_id" class="fact-item">
              <div class="fact-triple">
                <span class="fact-subject">{{ fact.subject_text }}</span>
                <span class="fact-predicate">{{ fact.predicate }}</span>
                <span class="fact-object">{{ fact.object_text }}</span>
              </div>
              <div v-if="periodText(fact)" class="fact-period">⏱ {{ periodText(fact) }}</div>
              <div class="fact-evidence" :title="fact.evidence_text">
                {{ fact.evidence_text || '（无原文依据）' }}
              </div>
              <div class="fact-meta">
                <a-tag v-if="fact.confidence != null" size="small" :color="confidenceColor(fact.confidence)">
                  {{ Math.round(fact.confidence * 100) }}%
                </a-tag>
                <a-tag v-if="fact.status === 'approved' && fact.invalidated_at" size="small" color="red">
                  已被取代
                </a-tag>
                <span class="fact-time">{{ formatTime(fact.decided_at || fact.recorded_at) }}</span>
                <span v-if="fact.decided_by" class="fact-by">
                  {{ fact.decided_by === 'auto' ? '自动批准' : fact.decided_by }}
                </span>
                <template v-if="!readonly && fact.status === 'pending'">
                  <span class="fact-actions">
                    <a-button
                      size="small"
                      type="primary"
                      :loading="decidingId === fact.fact_id && decidingAction === 'approve'"
                      @click="decide(fact, 'approve')"
                    >
                      批准
                    </a-button>
                    <a-button
                      size="small"
                      danger
                      :loading="decidingId === fact.fact_id && decidingAction === 'reject'"
                      @click="decide(fact, 'reject')"
                    >
                      驳回
                    </a-button>
                  </span>
                </template>
                <a-tag v-else-if="fact.status === 'approved' && !fact.invalidated_at" color="green" size="small"
                  >已入图谱</a-tag
                >
                <a-tag v-else-if="fact.status === 'rejected'" size="small">已驳回</a-tag>
              </div>
            </div>
          </div>
        </div>

        <a-pagination
          v-if="total > pageSize"
          v-model:current="page"
          size="small"
          :page-size="pageSize"
          :total="total"
          :show-size-changer="false"
          class="facts-pagination"
          @change="loadFacts"
        />
      </div>
    </div>
  </transition>
</template>

<script setup>
import { ref, reactive, watch } from 'vue'
import { RefreshCw, Loader2 } from '@lucide/vue'
import { Modal, message } from 'ant-design-vue'
import { governanceApi } from '@/apis/knowledge_api'

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  kbId: {
    type: String,
    default: ''
  },
  readonly: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['close', 'changed'])

const statusTabs = [
  { key: 'pending', label: '待审' },
  { key: 'approved', label: '已批准' },
  { key: 'rejected', label: '已驳回' },
  { key: 'invalidated', label: '已失效' },
  { key: 'all', label: '全部' }
]

const configForm = reactive({ enabled: false, auto_approve_threshold: 0.8 })
const configSaving = ref(false)
const activeStatus = ref('pending')
const facts = ref([])
const statusCounts = ref({})
const total = ref(0)
const page = ref(1)
const pageSize = 20
const factsLoading = ref(false)
const decidingId = ref('')
const decidingAction = ref('')
const asOfInput = ref('')
const asOfActive = ref('')

const reload = () => {
  loadConfig()
  loadFacts()
}

const loadConfig = async () => {
  if (!props.kbId) return
  try {
    const data = await governanceApi.getConfig(props.kbId)
    configForm.enabled = Boolean(data.config?.enabled)
    configForm.auto_approve_threshold = Number(data.config?.auto_approve_threshold ?? 0.8)
  } catch (e) {
    console.error('Failed to load governance config:', e)
  }
}

const loadFacts = async () => {
  if (!props.kbId) return
  factsLoading.value = true
  try {
    const params = {
      status: asOfActive.value ? undefined : activeStatus.value === 'all' ? 'all' : activeStatus.value,
      page: page.value,
      page_size: pageSize
    }
    if (asOfActive.value) {
      params.as_of = asOfActive.value
    }
    const data = await governanceApi.listFacts(props.kbId, params)
    facts.value = data.items || []
    statusCounts.value = data.status_counts || {}
    total.value = Number(data.total || 0)
  } catch (e) {
    console.error('Failed to load governance facts:', e)
    message.error(e?.response?.data?.detail || '加载知识事实列表失败')
  } finally {
    factsLoading.value = false
  }
}

const switchStatus = (key) => {
  asOfActive.value = ''
  if (activeStatus.value === key) return
  activeStatus.value = key
  page.value = 1
  loadFacts()
}

const runAsOfQuery = () => {
  const text = (asOfInput.value || '').trim()
  if (!text) {
    message.warning('请输入要查询的时点，如 2024-03-15')
    return
  }
  asOfActive.value = text
  page.value = 1
  loadFacts()
}

const clearAsOf = () => {
  asOfActive.value = ''
  asOfInput.value = ''
  page.value = 1
  loadFacts()
}

const saveConfig = async () => {
  configSaving.value = true
  try {
    const threshold = configForm.auto_approve_threshold
    await governanceApi.updateConfig(props.kbId, {
      enabled: configForm.enabled,
      auto_approve_threshold: threshold == null ? null : Number(threshold)
    })
    message.success('知识治理配置已保存')
    emit('changed')
  } catch (e) {
    console.error('Failed to save governance config:', e)
    message.error(e?.response?.data?.detail || '保存知识治理配置失败')
  } finally {
    configSaving.value = false
  }
}

const decide = (fact, action) => {
  const run = async () => {
    decidingId.value = fact.fact_id
    decidingAction.value = action
    try {
      const result = await governanceApi.decideFact(props.kbId, fact.fact_id, action)
      const superseded = Number(result?.fact?.superseded_count || 0)
      if (action === 'approve' && superseded > 0) {
        message.success(`事实已批准并写入图谱；同陈述旧版本已标记失效（${superseded} 条）`)
      } else {
        message.success(action === 'approve' ? '事实已批准并写入图谱' : '事实已驳回')
      }
      await loadFacts()
      emit('changed')
    } catch (e) {
      console.error('Failed to decide fact:', e)
      message.error(e?.response?.data?.detail || '裁决知识事实失败')
    } finally {
      decidingId.value = ''
      decidingAction.value = ''
    }
  }
  if (action === 'reject') {
    Modal.confirm({
      title: '驳回这条事实？',
      content: `${fact.subject_text} ${fact.predicate} ${fact.object_text}`,
      okText: '驳回',
      cancelText: '取消',
      onOk: run
    })
  } else {
    run()
  }
}

const confidenceColor = (confidence) => {
  if (confidence >= 0.8) return 'green'
  if (confidence >= 0.5) return 'orange'
  return 'red'
}

const formatTime = (value) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString()
}

const periodText = (fact) => {
  const fmt = (value, precision) => {
    if (!value) return ''
    const date = String(value)
    if (precision === 'year') return date.slice(0, 4)
    if (precision === 'month') return date.slice(0, 7)
    return date.slice(0, 10)
  }
  const start = fmt(fact.valid_from, fact.valid_from_precision)
  let end = fmt(fact.valid_to, fact.valid_to_precision)
  if (fact.valid_to_precision === 'unknown') end = '未知时间结束'
  if (start && end) return `${start} ~ ${end}`
  if (start) return `自 ${start}`
  if (end) return `至 ${end} 前有效`
  return ''
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      page.value = 1
      reload()
    }
  }
)
</script>

<style scoped>
.floating-governance-panel {
  position: absolute;
  top: 60px;
  right: 10px;
  width: 380px;
  max-height: calc(100% - 70px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--gray-200);
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-1000);
}

.panel-refresh-btn {
  color: var(--gray-600);
}

.panel-body {
  padding: 12px 14px;
}

.config-block {
  padding-bottom: 10px;
  margin-bottom: 10px;
  border-bottom: 1px solid var(--gray-200);
}

.config-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.config-label {
  color: var(--gray-700);
}

.config-save-btn {
  margin-top: 2px;
}

.config-hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--gray-500);
}

.as-of-block {
  padding-bottom: 10px;
  margin-bottom: 10px;
  border-bottom: 1px solid var(--gray-200);
}

.as-of-row {
  display: flex;
  gap: 6px;
}

.as-of-banner {
  margin-top: 6px;
  font-size: 12px;
  color: var(--gray-600);
  background: var(--gray-100);
  border-radius: 4px;
  padding: 4px 8px;
}

.counts-row {
  display: flex;
  gap: 6px;
  margin-bottom: 10px;
}

.count-chip {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 6px 2px;
  border-radius: 6px;
  border: 1px solid var(--gray-200);
  cursor: pointer;
  transition:
    background 0.2s,
    border-color 0.2s;
}

.count-chip.active {
  border-color: var(--gray-800);
  background: var(--gray-100);
}

.count-value {
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-1000);
}

.count-label {
  font-size: 11px;
  color: var(--gray-500);
}

.facts-list {
  min-height: 60px;
}

.facts-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px 0;
  color: var(--gray-500);
}

.fact-item {
  padding: 8px 0;
  border-bottom: 1px solid var(--gray-200);
}

.fact-item:last-child {
  border-bottom: none;
}

.fact-triple {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  color: var(--gray-1000);
  font-weight: 500;
}

.fact-predicate {
  color: var(--gray-500);
  font-size: 12px;
}

.fact-period {
  margin-top: 4px;
  font-size: 12px;
  color: var(--gray-700);
}

.fact-evidence {
  margin-top: 4px;
  font-size: 12px;
  color: var(--gray-600);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fact-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.fact-time {
  font-size: 11px;
  color: var(--gray-500);
}

.fact-by {
  font-size: 11px;
  color: var(--gray-500);
}

.fact-actions {
  display: inline-flex;
  gap: 6px;
  margin-left: auto;
}

.facts-pagination {
  margin-top: 10px;
  text-align: right;
}

.spin {
  animation: governance-spin 1s linear infinite;
}

@keyframes governance-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

.governance-slide-fade-enter-active,
.governance-slide-fade-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}

.governance-slide-fade-enter-from,
.governance-slide-fade-leave-to {
  opacity: 0;
  transform: translateX(12px);
}
</style>
