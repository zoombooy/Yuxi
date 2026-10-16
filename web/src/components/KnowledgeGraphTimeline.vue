<template>
  <transition name="timeline-fade">
    <div v-if="visible" class="graph-timeline-bar">
      <div class="tl-left">
        <a-button size="small" type="text" class="tl-close-btn" @click="$emit('close')">
          <X :size="14" />
        </a-button>
        <span class="tl-title">时间轴</span>
        <span class="tl-count">{{ loading ? '加载中…' : `时点事实 ${activeCount}` }}</span>
      </div>

      <div class="tl-center">
        <div class="tl-filters">
          <a-select
            v-model:value="predicateFilter"
            size="small"
            class="tl-predicate-select"
            :options="predicateOptions"
            placeholder="关系类型（全部）"
            allow-clear
            @change="onFilterChange"
          />
          <a-input
            v-model:value="entityFilter"
            size="small"
            class="tl-entity-input"
            placeholder="聚焦实体，如 WPS"
            allow-clear
            @press-enter="onFilterChange"
            @change="onEntityInputChange"
          />
          <a-button size="small" class="tl-event-toggle-btn" @click="showEvents = !showEvents">
            <List :size="14" />
            事件 {{ scopedEvents.length }}
          </a-button>
        </div>

        <div class="tl-slider-wrap">
          <div v-if="!disabled" class="tl-ticks">
            <span
              v-for="event in scopedEvents"
              :key="event.key"
              class="tl-tick"
              :class="{ active: isCurrentEvent(event) }"
              :style="{ left: tickPercent(event) + '%' }"
              :title="eventTitle(event)"
              @click="jumpToEvent(event)"
            ></span>
          </div>
          <input
            v-model.number="position"
            type="range"
            class="tl-slider"
            min="0"
            max="1000"
            step="1"
            :disabled="disabled"
            @input="onScrub"
          />
        </div>

        <div class="tl-range">
          <span>{{ formatShort(rangeMin) }}</span>
          <span class="tl-time" :class="{ live: atEnd }">{{ currentLabel }}</span>
          <span>{{ formatShort(rangeMax) }}</span>
        </div>
      </div>

      <div class="tl-right">
        <a-button size="small" class="tl-play-btn" :disabled="disabled" @click="togglePlay">
          <component :is="playing ? Pause : Play" :size="14" />
        </a-button>
        <a-button size="small" class="tl-now-btn" :disabled="disabled" @click="jumpToNow">现在</a-button>
      </div>

      <div v-if="showEvents && !disabled" class="tl-event-list">
        <div
          v-for="event in scopedEvents"
          :key="event.key"
          class="tl-event-item"
          :class="{ active: isCurrentEvent(event) }"
          @click="jumpToEvent(event)"
        >
          <div class="tl-event-time">{{ formatFull(event.time) }}</div>
          <div v-for="fact in event.facts" :key="fact.fact_id" class="tl-event-fact">
            <span class="tl-event-badge" :class="{ change: isChangeEvent(fact, event) }">
              {{ isChangeEvent(fact, event) ? '变更' : '新增' }}
            </span>
            {{ fact.subject_text }} {{ fact.predicate }} {{ fact.object_text }}
            <span v-if="fact.confidence != null" class="tl-event-conf">{{ Math.round(fact.confidence * 100) }}%</span>
          </div>
        </div>
        <div v-if="!scopedEvents.length" class="tl-event-empty">当前筛选下没有事件</div>
      </div>
    </div>
  </transition>
</template>

<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import { X, Play, Pause, List } from '@lucide/vue'
import { message } from 'ant-design-vue'
import { governanceApi } from '@/apis/knowledge_api'

const props = defineProps({
  visible: {
    type: Boolean,
    default: false
  },
  kbId: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['close', 'update'])

const facts = ref([])
const loading = ref(false)
const position = ref(1000)
const playing = ref(false)
const playTimer = ref(null)
const predicateFilter = ref(undefined)
const entityFilter = ref('')
const showEvents = ref(false)
const PAGE_SIZE = 100

const minTime = ref(0)
const maxTime = ref(0)

const disabled = computed(() => loading.value || scopedFacts.value.length === 0)
const atEnd = computed(() => position.value >= 1000)

// 筛选（谓词 + 实体模糊匹配）后的参与时间轴的事实
const scopedFacts = computed(() =>
  facts.value.filter((fact) => {
    if (predicateFilter.value && (fact.predicate || '') !== predicateFilter.value) return false
    const query = (entityFilter.value || '').trim().toLowerCase()
    if (query) {
      const subject = (fact.subject_text || '').toLowerCase()
      const object = (fact.object_text || '').toLowerCase()
      if (!subject.includes(query) && !object.includes(query)) return false
    }
    return true
  })
)

const predicateOptions = computed(() => {
  const counts = new Map()
  for (const fact of facts.value) {
    const predicate = fact.predicate || 'RELATED_TO'
    counts.set(predicate, (counts.get(predicate) || 0) + 1)
  }
  return Array.from(counts, ([value, count]) => ({
    value,
    label: `${value} (${count})`
  })).sort((a, b) => b.label.localeCompare(a.label))
})

// 事件 = 筛选范围内事实的入库时刻（按秒聚合）
const scopedEvents = computed(() => {
  const map = new Map()
  for (const fact of scopedFacts.value) {
    const time = new Date(fact.recorded_at)
    if (Number.isNaN(time.getTime())) continue
    const key = Math.floor(time.getTime() / 1000)
    if (!map.has(key)) map.set(key, { key, time, facts: [] })
    map.get(key).facts.push(fact)
  }
  return Array.from(map.values()).sort((a, b) => a.time.getTime() - b.time.getTime())
})

const rangeMin = computed(() => {
  const times = scopedFacts.value.map((fact) => new Date(fact.recorded_at).getTime())
  return times.length ? new Date(Math.min(...times)) : null
})

const rangeMax = computed(() => {
  const times = scopedFacts.value.map((fact) => new Date(fact.recorded_at).getTime())
  return times.length ? new Date(Math.max(...times)) : null
})

const currentTime = computed(() => {
  if (!rangeMin.value || !rangeMax.value) return null
  const span = rangeMax.value.getTime() - rangeMin.value.getTime()
  return new Date(rangeMin.value.getTime() + (span * position.value) / 1000)
})

const currentLabel = computed(() => {
  if (disabled.value) return '—'
  const label = formatFull(currentTime.value)
  return atEnd.value ? `${label}（现在）` : label
})

const activeCount = computed(() => {
  const time = currentTime.value
  if (!time) return 0
  return factsAt(time).length
})

function formatFull(date) {
  if (!date || Number.isNaN(date.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function formatShort(date) {
  if (!date || Number.isNaN(date.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function factsAt(time) {
  const ts = time.getTime()
  return scopedFacts.value.filter((fact) => {
    const recorded = new Date(fact.recorded_at).getTime()
    if (recorded > ts) return false
    if (fact.invalidated_at) {
      const invalidated = new Date(fact.invalidated_at).getTime()
      if (invalidated <= ts) return false
    }
    return true
  })
}

function isChangeEvent(fact, event) {
  // 同一主语+谓词在此事件之前已有不同宾语的记录 → 视为变更（含并存待裁决）
  const before = scopedFacts.value.filter((other) => {
    if (other.fact_id === fact.fact_id) return false
    if (other.predicate !== fact.predicate) return false
    if ((other.subject_text || '') !== (fact.subject_text || '')) return false
    if ((other.object_text || '') === (fact.object_text || '')) return false
    return new Date(other.recorded_at).getTime() < event.time.getTime()
  })
  return before.length > 0
}

function eventTitle(event) {
  const first = event.facts[0] || {}
  return `${formatFull(event.time)}：${event.facts.length} 条记录`
}

function tickPercent(event) {
  if (!rangeMin.value || !rangeMax.value) return 0
  const span = rangeMax.value.getTime() - rangeMin.value.getTime()
  if (span <= 0) return 0
  return Math.min(Math.max(((event.time.getTime() - rangeMin.value.getTime()) / span) * 100, 0), 100)
}

function isCurrentEvent(event) {
  const time = currentTime.value
  if (!time) return false
  return Math.abs(event.time.getTime() - time.getTime()) < 1000
}

function jumpToEvent(event) {
  stopPlay()
  const span = rangeMax.value.getTime() - rangeMin.value.getTime()
  if (span <= 0) return
  position.value = Math.round(((event.time.getTime() - rangeMin.value.getTime()) / span) * 1000)
  emitGraph()
}

function normalizeEntityKey(text) {
  return (text || '').trim().toLowerCase().replace(/\s+/g, ' ')
}

function buildGraphData(time) {
  const held = factsAt(time)
  const entityByKey = new Map()
  const nodes = []
  const edges = []

  const entityNodeId = (text, label) => {
    const key = normalizeEntityKey(text)
    if (!key) return null
    if (!entityByKey.has(key)) {
      const id = `entity:${entityByKey.size}:${key}`
      entityByKey.set(key, id)
      nodes.push({ id, name: text, type: label || '实体' })
    }
    return entityByKey.get(key)
  }

  for (const fact of held) {
    const sourceId = entityNodeId(fact.subject_text, fact.subject_label)
    const targetId = entityNodeId(fact.object_text, fact.object_label)
    if (!sourceId || !targetId || sourceId === targetId) continue
    edges.push({
      id: `fact:${fact.fact_id}`,
      source_id: sourceId,
      target_id: targetId,
      type: fact.predicate || 'RELATED_TO'
    })
  }
  return { nodes, edges, count: held.length }
}

function emitGraph() {
  const time = currentTime.value
  if (!time) return
  const graphData = buildGraphData(time)
  emit('update', {
    graphData: { nodes: graphData.nodes, edges: graphData.edges },
    count: graphData.count,
    time: time.toISOString()
  })
}

const onScrub = () => {
  stopPlay()
  emitGraph()
}

const onFilterChange = () => {
  stopPlay()
  position.value = 1000
  emitGraph()
}

let entityInputTimer = null
const onEntityInputChange = () => {
  clearTimeout(entityInputTimer)
  entityInputTimer = setTimeout(() => {
    onFilterChange()
  }, 400)
}

const jumpToNow = () => {
  stopPlay()
  position.value = 1000
  emitGraph()
}

const togglePlay = () => {
  if (playing.value) {
    stopPlay()
    return
  }
  if (!scopedEvents.value.length) return
  stopPlay()
  playing.value = true
  let eventIndex = scopedEvents.value.findIndex((event) => event.time.getTime() > (currentTime.value?.getTime() || 0))
  if (eventIndex < 0) eventIndex = 0
  const step = () => {
    if (!playing.value || eventIndex >= scopedEvents.value.length) {
      stopPlay()
      return
    }
    const event = scopedEvents.value[eventIndex]
    const span = rangeMax.value.getTime() - rangeMin.value.getTime()
    if (span > 0) {
      position.value = Math.round(((event.time.getTime() - rangeMin.value.getTime()) / span) * 1000)
    }
    emitGraph()
    eventIndex += 1
    playTimer.value = setTimeout(step, 900)
  }
  step()
}

const stopPlay = () => {
  playing.value = false
  if (playTimer.value) {
    clearTimeout(playTimer.value)
    playTimer.value = null
  }
}

const loadFacts = async () => {
  if (!props.kbId) return
  loading.value = true
  try {
    const collected = []
    let total = Infinity
    let page = 1
    while (collected.length < total && page <= 50) {
      const data = await governanceApi.listFacts(props.kbId, {
        status: 'all',
        page,
        page_size: PAGE_SIZE
      })
      total = Number(data.total || 0)
      const items = data.items || []
      collected.push(...items)
      if (!items.length) break
      page += 1
    }
    facts.value = collected.filter((fact) => fact.status === 'approved' && fact.recorded_at)
    predicateFilter.value = undefined
    entityFilter.value = ''
    position.value = 1000
    emitGraph()
  } catch (e) {
    console.error('Failed to load timeline facts:', e)
    message.error('加载时间轴事实失败')
  } finally {
    loading.value = false
  }
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      stopPlay()
      loadFacts()
    } else {
      stopPlay()
    }
  }
)

onUnmounted(() => {
  stopPlay()
  clearTimeout(entityInputTimer)
})
</script>

<style scoped>
.graph-timeline-bar {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 12px;
  z-index: 100;
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 4px 0px var(--shadow-2);
  font-size: 13px;
  flex-wrap: wrap;
}

.tl-left {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  padding-top: 2px;
}

.tl-close-btn {
  color: var(--gray-600);
}

.tl-title {
  font-weight: 600;
  color: var(--gray-1000);
}

.tl-count {
  color: var(--gray-500);
  font-size: 12px;
}

.tl-center {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 260px;
}

.tl-filters {
  display: flex;
  align-items: center;
  gap: 6px;
}

.tl-predicate-select {
  width: 180px;
}

.tl-entity-input {
  width: 160px;
}

.tl-event-toggle-btn {
  margin-left: auto;
}

.tl-slider-wrap {
  position: relative;
  padding-top: 5px;
}

.tl-ticks {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 6px;
  pointer-events: none;
}

.tl-tick {
  position: absolute;
  width: 8px;
  height: 8px;
  margin-left: -4px;
  border-radius: 50%;
  background: var(--gray-400, #999);
  border: 1px solid var(--color-bg-container, #fff);
  cursor: pointer;
  pointer-events: auto;
  transform: translateY(-2px);
}

.tl-tick.active {
  background: var(--gray-900, #333);
  transform: translateY(-2px) scale(1.3);
}

.tl-slider {
  width: 100%;
  accent-color: var(--gray-800, #555);
  cursor: pointer;
  margin: 0;
}

.tl-range {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  color: var(--gray-500);
}

.tl-time {
  font-size: 12px;
  color: var(--gray-800);
  font-variant-numeric: tabular-nums;
}

.tl-time.live {
  color: var(--gray-1000);
  font-weight: 600;
}

.tl-right {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  padding-top: 2px;
}

.tl-event-list {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  right: 0;
  max-height: 260px;
  overflow-y: auto;
  border-radius: 8px;
  background: var(--color-trans-light, rgba(255, 255, 255, 0.96));
  backdrop-filter: blur(12px);
  border: 1px solid var(--gray-100);
  box-shadow: 0 0 8px 0px var(--shadow-2);
  padding: 8px;
}

.tl-event-item {
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
}

.tl-event-item:hover {
  background: var(--gray-100);
}

.tl-event-item.active {
  background: var(--gray-100);
  border-left: 2px solid var(--gray-800);
}

.tl-event-time {
  font-size: 12px;
  color: var(--gray-800);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.tl-event-fact {
  font-size: 12px;
  color: var(--gray-700);
  padding-left: 8px;
  margin-top: 2px;
}

.tl-event-badge {
  display: inline-block;
  font-size: 11px;
  padding: 0 4px;
  margin-right: 6px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--gray-600);
}

.tl-event-badge.change {
  background: #fff1e0;
  color: #d46b08;
}

.tl-event-conf {
  color: var(--gray-500);
  margin-left: 6px;
}

.tl-event-empty {
  padding: 12px;
  text-align: center;
  color: var(--gray-500);
}

.timeline-fade-enter-active,
.timeline-fade-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}

.timeline-fade-enter-from,
.timeline-fade-leave-to {
  opacity: 0;
  transform: translateY(10px);
}
</style>
