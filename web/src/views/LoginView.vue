<template>
  <div class="login-page">
    <!-- 巨构变换背景（移植自 utopia LoginScene） -->
    <LoginScene :leaving="leaving" />

    <!-- 服务状态提示 -->
    <div v-if="serverStatus === 'error'" class="server-status-alert" role="alert">
      <exclamation-circle-icon class="alert-icon" :size="18" />
      <div class="alert-text">
        <div class="alert-title">服务端连接失败</div>
        <div class="alert-message">{{ serverError }}</div>
      </div>
      <a-button type="link" size="small" :loading="healthChecking" @click="checkServerHealth">
        重试
      </a-button>
    </div>

    <div class="login-column" :class="{ 'is-leaving': leaving }">
      <!-- 品牌区 -->
      <div class="brand-hero u-rise">
        <h1 class="brand-title">{{ brandName }}</h1>
        <p class="brand-tagline">
          {{ tagline }}
          <span class="brand-tagline-source">{{ taglineSource }}</span>
        </p>
      </div>

      <!-- 登录卡片 -->
      <div class="login-card u-rise" style="animation-delay: 90ms">
        <div v-if="!isFirstRun" class="segmented" role="tablist">
          <button
            type="button"
            role="tab"
            class="segmented-item"
            :class="{ active: mode === 'login' }"
            :aria-selected="mode === 'login'"
            @click="mode = 'login'"
          >
            登录
          </button>
          <button
            type="button"
            role="tab"
            class="segmented-item"
            :class="{ active: mode === 'register' }"
            :aria-selected="mode === 'register'"
            @click="mode = 'register'"
          >
            注册
          </button>
        </div>

        <!-- 注册：未开放自助注册 -->
        <div v-if="!isFirstRun && mode === 'register'" class="register-notice">
          自助注册未开放，请联系管理员创建账号。
        </div>

        <!-- 初始化管理员表单 -->
        <form v-else-if="isFirstRun" class="login-form" @submit.prevent="handleInitialize">
          <h2 class="init-title">系统初始化，请创建超级管理员</h2>
          <label class="field-label" for="init-uid">UID</label>
          <input
            id="init-uid"
            v-model="adminForm.uid"
            class="field-input"
            placeholder="请输入UID（3-20个字符）"
            maxlength="20"
            autocomplete="username"
          />
          <label class="field-label" for="init-phone">手机号（可选）</label>
          <input
            id="init-phone"
            v-model="adminForm.phone_number"
            class="field-input"
            placeholder="可用于登录，可不填写"
            maxlength="11"
          />
          <label class="field-label" for="init-password">密码</label>
          <input
            id="init-password"
            v-model="adminForm.password"
            type="password"
            class="field-input"
            placeholder="请输入密码"
            :minlength="MIN_PASSWORD_LENGTH"
            autocomplete="new-password"
          />
          <label class="field-label" for="init-confirm">确认密码</label>
          <input
            id="init-confirm"
            v-model="adminForm.confirmPassword"
            type="password"
            class="field-input"
            placeholder="请再次输入密码"
            autocomplete="new-password"
          />
          <p v-if="errorMessage" class="form-error" role="alert">{{ errorMessage }}</p>
          <button type="submit" class="primary-button" :disabled="loading">
            {{ loading ? '创建中…' : '创建管理员账户' }}
          </button>
        </form>

        <!-- 登录表单 -->
        <form v-else class="login-form" @submit.prevent="handleLogin">
          <input
            v-model="loginForm.loginId"
            class="field-input"
            placeholder="UID或手机号"
            autocomplete="username"
            required
          />
          <input
            v-model="loginForm.password"
            type="password"
            class="field-input"
            placeholder="密码"
            autocomplete="current-password"
            required
          />
          <p v-if="errorMessage" class="form-error" role="alert">{{ errorMessage }}</p>
          <button type="submit" class="primary-button" :disabled="loading || leaving || isLocked">
            <span v-if="isLocked">账户已锁定 {{ formatTime(lockRemainingTime) }}</span>
            <span v-else-if="loading || leaving">登录中…</span>
            <span v-else>登录</span>
          </button>
        </form>

        <!-- OIDC 登录选项 -->
        <div v-if="!isFirstRun && mode === 'login' && (oidcChecking || oidcEnabled)" class="third-party">
          <div class="divider"><span>或使用以下方式登录</span></div>
          <div v-if="oidcChecking" class="oidc-skeleton">
            <a-skeleton-button block size="large" :active="true" />
          </div>
          <button
            v-else
            type="button"
            class="secondary-button"
            :disabled="leaving || oidcLoading"
            @click="handleOIDCLogin"
          >
            <key-icon :size="16" class="oidc-icon" />
            {{ oidcLoading ? '跳转中…' : oidcButtonText }}
          </button>
        </div>
      </div>

      <!-- 页脚：惯用同意句式内嵌条款/隐私链接 -->
      <div class="login-footer u-rise" style="animation-delay: 180ms">
        <p class="footer-agree">
          继续即表示你同意<template v-if="showAgreementConsent"
            ><a class="u-link" :href="userAgreementUrl" target="_blank" rel="noopener noreferrer"
              >使用条款</a
            >，并已知悉<a
              class="u-link"
              :href="privacyPolicyUrl"
              target="_blank"
              rel="noopener noreferrer"
              >隐私政策</a
            >。</template
          >
          <template v-else>使用条款，并已知悉隐私政策。</template>
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useInfoStore } from '@/stores/info'
import { useAgentStore } from '@/stores/agent'
import { message } from 'ant-design-vue'
import { healthApi } from '@/apis/system_api'
import { authApi } from '@/apis/auth_api'
import {
  Key as KeyIcon,
  AlertCircle as ExclamationCircleIcon
} from '@lucide/vue'
import LoginScene from '@/components/LoginScene.vue'
import { tryAutoStartOIDC, sanitizeRedirect } from '@/utils/oidcAutoStart'
import { MIN_PASSWORD_LENGTH } from '@/utils/passwordValidation'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const infoStore = useInfoStore()
const agentStore = useAgentStore()

// 品牌展示数据（固定品牌：Topuria）
const brandName = 'Topuria'
const tagline = '企业级双时态知识'
const taglineSource = '知识库与智能体平台'
const userAgreementUrl = computed(() => infoStore.footer?.user_agreement_url?.trim() || '')
const privacyPolicyUrl = computed(() => infoStore.footer?.privacy_policy_url?.trim() || '')
const showAgreementConsent = computed(() => Boolean(userAgreementUrl.value && privacyPolicyUrl.value))

// 状态
const mode = ref('login')
const isFirstRun = ref(false)
const loading = ref(false)
const leaving = ref(false)
const errorMessage = ref('')
const serverStatus = ref('loading')
const serverError = ref('')
const healthChecking = ref(false)

// OIDC 相关状态
const oidcEnabled = ref(false)
const oidcLoading = ref(false)
const oidcChecking = ref(true)
const oidcButtonText = ref('OIDC 登录')

// 登录锁定相关状态
const isLocked = ref(false)
const lockRemainingTime = ref(0)
const lockCountdown = ref(null)

// 登录表单
const loginForm = reactive({
  loginId: '', // 支持uid或phone_number登录
  password: ''
})

// 管理员初始化表单
const adminForm = reactive({
  uid: '',
  password: '',
  confirmPassword: '',
  phone_number: ''
})

// 清理倒计时器
const clearLockCountdown = () => {
  if (lockCountdown.value) {
    clearInterval(lockCountdown.value)
    lockCountdown.value = null
  }
}

// 启动锁定倒计时
const startLockCountdown = (remainingSeconds) => {
  clearLockCountdown()
  isLocked.value = true
  lockRemainingTime.value = remainingSeconds

  lockCountdown.value = setInterval(() => {
    lockRemainingTime.value--
    if (lockRemainingTime.value <= 0) {
      clearLockCountdown()
      isLocked.value = false
      errorMessage.value = ''
    }
  }, 1000)
}

// 格式化时间显示
const formatTime = (seconds) => {
  if (seconds < 60) {
    return `${seconds}秒`
  } else if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60)
    const remainingSeconds = seconds % 60
    return `${minutes}分${remainingSeconds}秒`
  } else if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    return `${hours}小时${minutes}分钟`
  } else {
    const days = Math.floor(seconds / 86400)
    const hours = Math.floor((seconds % 86400) / 3600)
    return `${days}天${hours}小时`
  }
}

// 处理登录
const handleLogin = async () => {
  if (!loginForm.loginId.trim()) {
    errorMessage.value = '请输入UID或手机号'
    return
  }
  if (!loginForm.password) {
    errorMessage.value = '请输入密码'
    return
  }
  // 如果当前被锁定，不允许登录
  if (isLocked.value) {
    message.warning(`账户被锁定，请等待 ${formatTime(lockRemainingTime.value)}`)
    return
  }

  try {
    loading.value = true
    errorMessage.value = ''
    clearLockCountdown()

    await userStore.login({
      loginId: loginForm.loginId,
      password: loginForm.password
    })

    message.success('登录成功')

    // 获取重定向路径
    const redirectPath = sessionStorage.getItem('redirect') || '/'
    sessionStorage.removeItem('redirect') // 清除重定向信息

    // 谢幕：卡片上浮淡出、巨构放大穿越，再进入系统
    leaving.value = true
    window.setTimeout(async () => {
      if (redirectPath === '/') {
        // 统一跳转到聊天页面（管理员与普通用户共享同一聊天界面）
        try {
          await agentStore.initialize()
          router.push('/agent')
        } catch (error) {
          console.error('获取智能体信息失败:', error)
          router.push('/agent')
        }
      } else {
        router.push(redirectPath)
      }
    }, 650)
  } catch (error) {
    leaving.value = false
    console.error('登录失败:', error)

    // 检查是否是锁定错误（HTTP 423）
    if (error.status === 423) {
      let remainingTime = 0
      if (error.headers && error.headers.get) {
        const lockRemainingHeader = error.headers.get('X-Lock-Remaining')
        if (lockRemainingHeader) {
          remainingTime = parseInt(lockRemainingHeader)
        }
      }
      if (remainingTime === 0) {
        const lockTimeMatch = error.message.match(/(\d+)\s*秒/)
        if (lockTimeMatch) {
          remainingTime = parseInt(lockTimeMatch[1])
        }
      }
      if (remainingTime > 0) {
        startLockCountdown(remainingTime)
        errorMessage.value = `由于多次登录失败，账户已被锁定 ${formatTime(remainingTime)}`
      } else {
        errorMessage.value = error.message || '账户被锁定，请稍后再试'
      }
    } else {
      errorMessage.value = error.message || '登录失败，请检查用户名和密码'
    }
  } finally {
    loading.value = false
  }
}

// 处理 OIDC 登录
const handleOIDCLogin = async () => {
  try {
    oidcLoading.value = true
    errorMessage.value = ''

    const response = await authApi.getOIDCLoginUrl()
    if (response.login_url) {
      const redirectPath =
        sessionStorage.getItem('redirect') || router.currentRoute.value.query.redirect || '/'
      sessionStorage.setItem('oidc_redirect', redirectPath)
      window.location.href = response.login_url
    } else {
      errorMessage.value = '获取 OIDC 登录地址失败'
    }
  } catch (error) {
    console.error('OIDC 登录失败:', error)
    errorMessage.value = error.message || 'OIDC 登录失败，请重试'
  } finally {
    oidcLoading.value = false
  }
}

// 检查 OIDC 配置
const checkOIDCConfig = async () => {
  oidcChecking.value = true
  try {
    const config = await authApi.getOIDCConfig()
    oidcEnabled.value = config.enabled
    if (config.provider_name) {
      oidcButtonText.value = config.provider_name
    }
    return config
  } catch (error) {
    console.error('检查 OIDC 配置失败:', error)
    oidcEnabled.value = false
    return null
  } finally {
    oidcChecking.value = false
  }
}

// 处理初始化管理员
const handleInitialize = async () => {
  const uid = adminForm.uid.trim()
  if (!uid) {
    errorMessage.value = '请输入UID'
    return
  }
  if (!/^[a-zA-Z0-9_]+$/.test(uid)) {
    errorMessage.value = 'UID只能包含字母、数字和下划线'
    return
  }
  if (uid.length < 3 || uid.length > 20) {
    errorMessage.value = 'UID长度必须在3-20个字符之间'
    return
  }
  if (adminForm.phone_number && !/^1[3-9]\d{9}$/.test(adminForm.phone_number.trim())) {
    errorMessage.value = '请输入正确的手机号格式'
    return
  }
  if (!adminForm.password || adminForm.password.length < MIN_PASSWORD_LENGTH) {
    errorMessage.value = `密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符`
    return
  }
  if (adminForm.password !== adminForm.confirmPassword) {
    errorMessage.value = '两次输入的密码不一致'
    return
  }

  try {
    loading.value = true
    errorMessage.value = ''

    await userStore.initialize({
      uid,
      password: adminForm.password,
      phone_number: adminForm.phone_number || null
    })

    message.success('管理员账户创建成功')
    router.push('/')
  } catch (error) {
    console.error('初始化失败:', error)
    errorMessage.value = error.message || '初始化失败，请重试'
  } finally {
    loading.value = false
  }
}

// 检查是否是首次运行
const checkFirstRunStatus = async () => {
  try {
    loading.value = true
    const isFirst = await userStore.checkFirstRun()
    isFirstRun.value = isFirst
  } catch (error) {
    console.error('检查首次运行状态失败:', error)
    errorMessage.value = '系统出错，请稍后重试'
  } finally {
    loading.value = false
  }
}

// 检查服务器健康状态
const checkServerHealth = async () => {
  try {
    healthChecking.value = true
    const response = await healthApi.checkHealth()
    if (response.status === 'ok') {
      serverStatus.value = 'ok'
    } else {
      serverStatus.value = 'error'
      serverError.value = response.message || '服务端状态异常'
    }
  } catch (error) {
    console.error('检查服务器健康状态失败:', error)
    serverStatus.value = 'error'
    serverError.value = error.message || '无法连接到服务端，请检查网络连接'
  } finally {
    healthChecking.value = false
  }
}

// 组件挂载时
onMounted(async () => {
  // 如果已登录，按 redirect 参数跳转（不固定跳首页）
  if (userStore.isLoggedIn) {
    router.push(sanitizeRedirect(route.query.redirect))
    return
  }

  // 显示 OIDC 认证失败的错误信息（由后端重定向携带）
  if (route.query.oidc_error) {
    errorMessage.value = String(route.query.oidc_error)
  }

  // 首先检查服务器健康状态
  await checkServerHealth()

  // 检查是否是首次运行
  await checkFirstRunStatus()

  // 如果处于首次运行状态，不需要 OIDC 自动登录
  if (isFirstRun.value) {
    return
  }

  // 检查 OIDC 配置完成后，尝试自动触发 OIDC 登录（跨系统跳转场景）
  const config = await checkOIDCConfig()
  if (config && config.enabled) {
    const autoStarted = await tryAutoStartOIDC(async () => await authApi.getOIDCLoginUrl(), config)
    // 如果已发起 OIDC 跳转，页面会被重定向，不需要继续
    if (autoStarted) return
  }
})

// 组件卸载时清理定时器
onUnmounted(() => {
  clearLockCountdown()
})
</script>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: #fafafa;
}

.login-column {
  position: relative;
  z-index: 10;
  width: 100%;
  max-width: 384px;
}

/* ---- 品牌区 ---- */
.brand-hero {
  margin-bottom: 32px;
  text-align: center;
}

.brand-title {
  font-size: 48px;
  line-height: 1;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: #171717;
}

.brand-tagline {
  margin-top: 8px;
  font-size: 14px;
  line-height: 22px;
  color: #5f5f5f;
}

.brand-tagline-source {
  margin-left: 8px;
  font-size: 12px;
  line-height: 16px;
  color: #8a8a8a;
}

/* ---- 卡片 ---- */
.login-card {
  background: #ffffff;
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-radius: 14px;
  padding: 24px;
}

.segmented {
  display: flex;
  gap: 4px;
  background: #f6f6f6;
  padding: 4px;
  border-radius: 10px;
  margin-bottom: 24px;
}

.segmented-item {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  border: none;
  background: transparent;
  border-radius: 10px;
  padding: 4px 12px;
  font-size: 13px;
  line-height: 20px;
  font-weight: 500;
  color: #5f5f5f;
  cursor: pointer;
  transition:
    background-color 0.15s,
    color 0.15s;
}

.segmented-item:hover {
  background: #f0f0f0;
  color: #171717;
}

.segmented-item.active {
  background: #e9e9e9;
  color: #171717;
}

.register-notice {
  padding: 28px 8px;
  text-align: center;
  font-size: 13px;
  line-height: 20px;
  color: #5f5f5f;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.init-title {
  font-size: 16px;
  font-weight: 600;
  color: #171717;
  margin-bottom: 4px;
}

.field-label {
  font-size: 12px;
  line-height: 16px;
  color: #5f5f5f;
  margin-top: 4px;
}

.field-input {
  width: 100%;
  height: 40px;
  border: 1px solid #e4e4e4;
  border-radius: 10px;
  padding: 0 12px;
  font-size: 14px;
  color: #171717;
  background: #fff;
  outline: none;
  transition: border-color 0.15s;
}

.field-input::placeholder {
  color: #9b9b9b;
}

.field-input:focus {
  border-color: #008580;
}

/* Chrome 自动填充的蓝底与中性设计冲突：压回白色 */
.field-input:-webkit-autofill,
.field-input:-webkit-autofill:hover,
.field-input:-webkit-autofill:focus {
  -webkit-box-shadow: 0 0 0 1000px #fff inset;
  -webkit-text-fill-color: #171717;
  transition: background-color 5000s ease-in-out 0s;
}

.form-error {
  font-size: 13px;
  line-height: 20px;
  color: #b3261e;
}

.primary-button {
  width: 100%;
  height: 40px;
  border: none;
  border-radius: 10px;
  background: #008580;
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s;
}

.primary-button:hover:not(:disabled) {
  background: #00746f;
}

.primary-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* ---- OIDC ---- */
.third-party {
  margin-top: 4px;
}

.divider {
  margin: 16px 0;
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
  line-height: 16px;
  color: #5f5f5f;
}

.divider::before,
.divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: rgba(0, 0, 0, 0.08);
}

.secondary-button {
  width: 100%;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border: 1px solid rgba(0, 0, 0, 0.14);
  border-radius: 10px;
  background: #fff;
  color: #171717;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s;
}

.secondary-button:hover:not(:disabled) {
  background: #f5f5f5;
}

.secondary-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* ---- 页脚 ---- */
.login-footer {
  margin-top: 24px;
  text-align: center;
}

.footer-agree {
  font-size: 12px;
  line-height: 20px;
  color: #5f5f5f;
  text-wrap: balance;
}

.u-link {
  color: #171717;
  text-decoration: underline;
  text-underline-offset: 3px;
  text-decoration-color: rgba(23, 23, 23, 0.3);
  white-space: nowrap;
}

.u-link:hover {
  text-decoration-color: rgba(23, 23, 23, 0.8);
}

/* ---- 服务状态提示 ---- */
.server-status-alert {
  position: fixed;
  top: 16px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 10px;
  background: #fff;
  border: 1px solid rgba(0, 0, 0, 0.08);
  border-left: 3px solid #b3261e;
  border-radius: 10px;
  padding: 10px 14px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
}

.alert-icon {
  color: #b3261e;
  flex-shrink: 0;
}

.alert-title {
  font-size: 13px;
  font-weight: 600;
  color: #171717;
}

.alert-message {
  font-size: 12px;
  color: #5f5f5f;
}

/* ---- 入场 / 谢幕动效（移植自 utopia styles.css） ---- */
@keyframes u-rise-kf {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.u-rise {
  animation: u-rise-kf 0.7s cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes u-depart-kf {
  to {
    opacity: 0;
    transform: translateY(-14px);
  }
}

.login-column.is-leaving {
  animation: u-depart-kf 0.55s cubic-bezier(0.5, 0, 0.9, 0.4) forwards;
}

@media (prefers-reduced-motion: reduce) {
  .u-rise,
  .login-column.is-leaving {
    animation: none;
  }
}
</style>
