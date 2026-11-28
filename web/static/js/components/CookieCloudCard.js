import { defineComponent, reactive, ref, computed, onMounted } from 'vue';
import { ElMessage } from 'element-plus';
import { api } from '../api.js';

/**
 * CookieCloud 同步卡片：站点配置页顶部使用。
 *
 * 凭据（服务器 / UUID / 密码）+ 预览 / 补全 PT 站点 / 同步到站点。
 * 同步或补全成功后向父页面发 changed 事件，让站点列表立即刷新。
 */
export default defineComponent({
    name: 'CookieCloudCard',
    emits: ['changed'],
    setup(props, { emit }) {
        const form = reactive({
            cookiecloud: { server: '', uuid: '', password: '' },
        });
        const loading = ref(false);
        const saving = ref(false);
        const ccLoading = ref(false);
        const ccPreview = ref(null);
        // 密码输入框始终从空开始；是否已保存由后端 has_password 决定。
        // passwordDirty 表示用户改过输入框，clearPassword 表示明确点了“清除已保存密码”。
        const hasPassword = ref(false);
        const passwordDirty = ref(false);
        const clearPassword = ref(false);
        // 预览默认只列 PT 站：CookieCloud 里一百多个域名绝大多数是普通网站，混在一起没法看
        const ccOnlyPt = ref(true);
        const ccDomains = computed(() => {
            const list = (ccPreview.value && ccPreview.value.domains) || [];
            return ccOnlyPt.value ? list.filter((d) => d && d.pt) : list;
        });

        async function load() {
            loading.value = true;
            try {
                const res = await api.get('/api/security');
                const cc = res?.cookiecloud || {};
                // 不把 password 回填到输入框：未修改时保持空，已保存状态只看 has_password。
                form.cookiecloud.server = cc.server || '';
                form.cookiecloud.uuid = cc.uuid || '';
                form.cookiecloud.password = '';
                hasPassword.value = !!cc.has_password;
                passwordDirty.value = false;
                clearPassword.value = false;
            } catch (e) {
                ElMessage.error(e.message || 'CookieCloud 配置加载失败');
            } finally {
                loading.value = false;
            }
        }

        function onPasswordInput() {
            passwordDirty.value = true;
            clearPassword.value = false;
        }

        function markClearPassword() {
            clearPassword.value = true;
            passwordDirty.value = true;
            form.cookiecloud.password = '';
        }

        function ccPayload() {
            let password = '';
            if (clearPassword.value) {
                // 明确清空：回传空串，后端会真正删掉旧密码
                password = '';
            } else if (passwordDirty.value) {
                password = form.cookiecloud.password || '';
            } else if (hasPassword.value) {
                // 没改过输入框：用 *** 哨兵让后端沿用旧密码
                password = '***';
            }
            return {
                server: form.cookiecloud.server || '',
                uuid: form.cookiecloud.uuid || '',
                password,
            };
        }

        async function saveCookieCloud() {
            saving.value = true;
            try {
                const res = await api.put('/api/security/cookiecloud', ccPayload());
                ElMessage.success(res?.message || '已保存');
                await load();
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                saving.value = false;
            }
        }

        async function previewCookieCloud() {
            if (!form.cookiecloud.uuid && !form.cookiecloud.server) {
                ElMessage.warning('请先填 CookieCloud 的 UUID');
                return;
            }
            ccLoading.value = true;
            try {
                const res = await api.post('/api/cookiecloud/preview', ccPayload());
                if (!res?.ok) {
                    ccPreview.value = null;
                    ElMessage.error(res?.message || '拉取失败');
                    return;
                }
                ccPreview.value = res;
                ElMessage.success(`拉到 ${res.total_domains} 个域名、共 ${res.total_cookies} 个 cookie，其中 ${res.pt_domains || 0} 个是 PT 站`);
            } catch (e) {
                ccPreview.value = null;
                ElMessage.error(e.message || '请求失败');
            } finally {
                ccLoading.value = false;
            }
        }

        async function syncCookieCloud() {
            ccLoading.value = true;
            try {
                const res = await api.post('/api/cookiecloud/sync', ccPayload());
                if (!res?.ok) {
                    ElMessage.error(res?.message || '同步失败');
                    return;
                }
                const detail = (res.synced || [])
                    .map(s => `${s.site}(${(s.domains || []).length} 个域 / ${s.cookies} 个 cookie)`)
                    .join('、');
                ElMessage.success(`${res.message}${detail ? '：' + detail : ''}`);
                if (res.skipped?.length) {
                    ElMessage.warning(res.skipped.map(s => `${s.site} 未同步：${s.reason}`).join('；'));
                }
                await load();
                emit('changed');
            } catch (e) {
                ElMessage.error(e.message || '请求失败');
            } finally {
                ccLoading.value = false;
            }
        }

        async function addPtSites() {
            ccLoading.value = true;
            try {
                const res = await api.post('/api/cookiecloud/add-pt-sites', ccPayload());
                if (!res?.ok) {
                    ElMessage.warning(res?.message || '没有新增站点');
                } else {
                    const detail = (res.added || []).map(a => a.site).join('、');
                    ElMessage.success(`${res.message}${detail ? '：' + detail : ''}`);
                    await load();
                    emit('changed');
                }
                const other = (res?.skipped || []).filter(s => !String(s.reason || '').includes('已有站点'));
                if (other.length) {
                    ElMessage.warning(other.map(s => `${s.domain} 跳过：${s.reason}`).join('；'));
                }
            } catch (e) {
                ElMessage.error(e.message || '请求失败');
            } finally {
                ccLoading.value = false;
            }
        }

        onMounted(load);

        return {
            form, loading, saving, ccLoading, ccPreview, ccOnlyPt, ccDomains,
            hasPassword, clearPassword, onPasswordInput, markClearPassword,
            saveCookieCloud, previewCookieCloud, syncCookieCloud, addPtSites,
        };
    },
    template: `
        <el-card shadow="hover" class="cc-card" style="margin-bottom:16px;" v-loading="loading">
            <template #header>
                <div class="card-header-row">
                    <strong>CookieCloud 同步</strong>
                    <div class="cc-actions">
                        <el-button size="small" plain :loading="ccLoading" @click="previewCookieCloud">预览</el-button>
                        <el-button size="small" :loading="ccLoading" @click="addPtSites">补全 PT 站点</el-button>
                        <el-button size="small" type="primary" :loading="ccLoading" @click="syncCookieCloud">同步到站点</el-button>
                    </div>
                </div>
            </template>
            <el-form label-position="top">
                <div class="form-grid cc-grid">
                    <el-form-item label="服务器地址">
                        <el-input v-model="form.cookiecloud.server" placeholder="留空用公共服务器" />
                    </el-form-item>
                    <el-form-item label="UUID">
                        <el-input v-model="form.cookiecloud.uuid" placeholder="浏览器插件里显示的 UUID" />
                    </el-form-item>
                    <el-form-item label="密码">
                        <el-input
                            v-model="form.cookiecloud.password"
                            type="password"
                            show-password
                            :placeholder="clearPassword ? '保存后将清除已保存密码' : (hasPassword ? '已保存密码，留空则不变' : '请输入 CookieCloud 密码')"
                            @input="onPasswordInput" />
                        <div v-if="hasPassword && !clearPassword" class="field-help">
                            已保存密码，输入框保持为空；<a href="#" @click.prevent="markClearPassword">清除已保存密码</a>
                        </div>
                        <div v-else-if="clearPassword" class="field-help">
                            保存后将清除已保存密码。
                        </div>
                    </el-form-item>
                </div>
                <div class="form-stack">
                    <div class="password-actions">
                        <el-button type="primary" :loading="saving" @click="saveCookieCloud">保存凭据</el-button>
                    </div>
                    <div v-if="ccPreview" class="cc-preview">
                        <div class="cc-preview-title">
                            <span>拉到 {{ ccPreview.total_domains }} 个域名、共 {{ ccPreview.total_cookies }} 个 cookie<template v-if="ccPreview.pt_domains">，其中 {{ ccPreview.pt_domains }} 个是 PT 站</template><template v-if="ccPreview.pt_names">（已加载 {{ ccPreview.pt_names }} 个站点的中文名）</template></span>
                            <el-checkbox v-model="ccOnlyPt" size="small">只看 PT 站</el-checkbox>
                        </div>
                        <div v-if="!ccDomains.length" class="cc-empty">没有 PT 站</div>
                        <div v-for="d in ccDomains" :key="d.domain" class="cc-domain-row">
                            <span class="cc-domain">
                                <i v-if="d.pt" class="cc-pt-tag">PT</i>{{ d.domain }}
                            </span>
                            <span class="cc-count">{{ d.cookies }} 个</span>
                            <span class="cc-match" :class="{ 'cc-match-none': !d.matched_sites.length }">
                                <template v-if="d.matched_sites.length">→ {{ d.matched_sites.join('、') }}</template>
                                <template v-else-if="d.pt_skip">{{ d.pt_skip }}</template>
                                <template v-else-if="d.pt && d.pt_name">→ {{ d.pt_name }}<span class="cc-add-hint">可添加</span></template>
                                <template v-else-if="d.pt">可自动添加</template>
                                <template v-else>未匹配站点</template>
                            </span>
                        </div>
                    </div>
                    <div class="field-help">
                        公共服务器地址 https://cookie.scy.cc.cd，UUID / 密码请自行设置。
                    </div>
                </div>
            </el-form>
        </el-card>
    `,
});
