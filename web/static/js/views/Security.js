import { defineComponent, reactive, ref, computed, onMounted } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { api } from '../api.js';
import PasswordCard from '../components/PasswordCard.js';

/**
 * 系统设置页。
 *
 * 四块内容：
 * 1. CookieCloud 同步 —— 凭据（服务器 / UUID / 密码）+ 预览 / 补全 PT 站点 / 同步到站点
 * 2. Web 访问        —— 登录密码（PasswordCard 组件）
 * 3. 备份与恢复      —— 一键导出全部配置数据；换设备导入恢复
 * 4. 下载与更新节点  —— 安装包走哪个节点（CF / 海外），更新清单一律自动选最快
 *
 * 反馈与联系已经迁到「详单」页（站点适配与测试账号都在那边）。
 */

export default defineComponent({
    name: 'Security',
    components: { PasswordCard },
    setup() {
        const form = reactive({
            cookiecloud: { server: '', uuid: '', password: '' },
        });
        const loading = ref(false);
        const saving = ref(false);
        const ccLoading = ref(false);
        const ccPreview = ref(null);
        // 预览默认只列 PT 站：CookieCloud 里一百多个域名绝大多数是普通网站，混在一起没法看
        const ccOnlyPt = ref(true);
        const ccDomains = computed(() => {
            const list = (ccPreview.value && ccPreview.value.domains) || [];
            return ccOnlyPt.value ? list.filter((d) => d && d.pt) : list;
        });
        const loginRequired = ref(false);

        async function load() {
            loading.value = true;
            try {
                const res = await api.get('/api/security');
                if (res?.cookiecloud) Object.assign(form.cookiecloud, res.cookiecloud);
                loginRequired.value = !!res?.web?.login_required;
            } catch (e) {
                ElMessage.error(e.message || '加载失败');
            } finally {
                loading.value = false;
            }
        }

        function ccPayload() {
            return {
                server: form.cookiecloud.server || '',
                uuid: form.cookiecloud.uuid || '',
                password: form.cookiecloud.password || '',
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

        // ---- 备份与恢复 ----
        const exporting = ref(false);
        const importing = ref(false);
        const importDone = ref(null);
        const backupInfo = ref(null);

        async function loadBackup() {
            try {
                backupInfo.value = await api.get('/api/backup/info');
            } catch (e) {
                // 备份信息只用于展示，失败不打扰用户
            }
        }

        async function exportBackup() {
            exporting.value = true;
            try {
                // 用 fetch + blob：失败时能把后端的错误文案留在页面上，而不是把标签页跳到一段 JSON
                const res = await fetch('/api/backup/export', { credentials: 'same-origin' });
                if (res.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.error || `导出失败 (${res.status})`);
                }
                const blob = await res.blob();
                const cd = res.headers.get('Content-Disposition') || '';
                const hit = /filename="?([^";]+)"?/i.exec(cd);
                const name = (hit && hit[1]) || `ptbrush-backup-${Date.now()}.zip`;
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = name;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(url), 2000);
                ElMessage.success(`备份已导出：${name}（${(blob.size / 1048576).toFixed(1)} MB）`);
                await loadBackup();
            } catch (e) {
                ElMessage.error(e.message || '导出失败');
            } finally {
                exporting.value = false;
            }
        }

        async function pickBackup(uploadFile) {
            const raw = uploadFile && uploadFile.raw;
            if (!raw) return;
            if (!/\.zip$/i.test(raw.name || '')) {
                ElMessage.warning('请选择 .zip 备份包');
                return;
            }
            importing.value = true;
            try {
                const fd = new FormData();
                fd.append('file', raw, raw.name);
                const res = await fetch('/api/backup/import', {
                    method: 'POST',
                    body: fd,
                    credentials: 'same-origin',
                });
                const data = await res.json().catch(() => ({}));
                if (res.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                if (!res.ok) throw new Error(data.error || `导入失败 (${res.status})`);
                importDone.value = data;
                ElMessage.success(data.message || '导入完成');
                await loadBackup();
                // 备份包里带着 [update] 段，导入后节点选择可能变了，顺手同步一下界面（容器版没有这块）
                if (portable.value) await loadNodes(false);
            } catch (e) {
                ElMessage.error(e.message || '导入失败');
            } finally {
                importing.value = false;
            }
        }

        async function restartApp() {
            try {
                await ElMessageBox.confirm(
                    '导入后要重启 PTBrush，站点、下载器和数据库才会全部按新配置加载。现在重启吗？',
                    '重启生效',
                    { type: 'warning', confirmButtonText: '立即重启', cancelButtonText: '稍后手动重启' },
                );
            } catch (e) {
                return;
            }
            try {
                await api.post('/api/backup/restart');
                ElMessage.success('正在重启，约 5 秒后自动刷新页面');
                setTimeout(() => location.reload(), 5000);
            } catch (e) {
                ElMessage.error(e.message || '重启请求失败');
            }
        }

        // ---- 下载 / 更新节点 ----
        const nodeForm = ref('cf');
        const nodeRows = ref([]);
        const nodeSaving = ref(false);
        const nodeProbing = ref(false);
        // 只有 Windows 便携版能自己下载 zip 替换升级；容器版把整块收成一条升级说明
        const portable = ref(true);

        async function loadNodes(probe) {
            try {
                const res = await api.get('/api/update/nodes' + (probe ? '?probe=1' : ''));
                nodeForm.value = res.current || 'cf';
                nodeRows.value = res.nodes || [];
            } catch (e) {
                if (probe) ElMessage.error(e.message || '测速失败');
            }
        }

        async function probeNodes() {
            nodeProbing.value = true;
            try {
                await loadNodes(true);
            } finally {
                nodeProbing.value = false;
            }
        }

        async function saveNode() {
            nodeSaving.value = true;
            try {
                const res = await api.post('/api/update/node', { node: nodeForm.value });
                ElMessage.success(res.message || '下载节点已保存');
                await loadNodes(false);
            } catch (e) {
                ElMessage.error(e.message || '保存失败');
            } finally {
                nodeSaving.value = false;
            }
        }

        onMounted(async () => {
            load();
            loadBackup();
            try {
                const info = await api.get('/api/version');
                // 老版本后端没有 portable 字段，按便携版处理，保持原行为
                portable.value = info.portable !== false;
            } catch (e) {
                // ignore
            }
            if (portable.value) loadNodes(false);
        });

        return {
            form, loading, saving, ccLoading, ccPreview, ccOnlyPt, ccDomains, loginRequired,
            load, saveCookieCloud, previewCookieCloud, syncCookieCloud, addPtSites,
            exporting, importing, importDone, backupInfo,
            exportBackup, pickBackup, restartApp,
            nodeForm, nodeRows, nodeSaving, nodeProbing, portable,
            saveNode, probeNodes,
        };
    },
    template: `
        <div class="sites-page" v-loading="loading">
            <div class="sites-header">
                <div>
                    <div class="sites-title">系统设置</div>
                </div>
            </div>

            <el-card shadow="hover" style="margin-bottom:16px;">
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
                            <el-input v-model="form.cookiecloud.password" type="password" show-password placeholder="已保存的密码显示为 ***" />
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

            <div style="margin-bottom:16px;">
                <PasswordCard :login-required="loginRequired" />
            </div>

            <el-card shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>备份与恢复</strong>
                    </div>
                </template>
                <div class="backup-block">
                    <div class="backup-desc">
                        把站点配置、下载器、CookieCloud 凭据、账号数据与统计整体打包成一个 zip；换设备后在新的 PTBrush 里导入这个包，就能恢复成现在这台机器的状态。
                    </div>
                    <div class="backup-actions">
                        <el-button type="primary" :loading="exporting" @click="exportBackup">导出备份</el-button>
                        <el-upload
                            :show-file-list="false"
                            :auto-upload="false"
                            accept=".zip"
                            :on-change="pickBackup">
                            <el-button :loading="importing">导入备份</el-button>
                        </el-upload>
                        <el-button v-if="importDone" type="warning" plain @click="restartApp">重启生效</el-button>
                    </div>
                    <div v-if="backupInfo" class="backup-meta">
                        当前数据 {{ backupInfo.files.length }} 个文件（不含日志与升级包）
                        <template v-if="backupInfo.backups && backupInfo.backups.length">
                            <span class="backup-sep">·</span>
                            最近一次备份 {{ backupInfo.backups[0].time }}
                        </template>
                    </div>
                    <div v-if="importDone" class="backup-hint">
                        已导入 {{ (importDone.replaced || []).length }} 个文件<template v-if="importDone.auto_backup">，导入前的数据自动备份为 {{ importDone.auto_backup }}</template>。重启后全部生效。
                    </div>
                </div>
            </el-card>

            <el-card shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>{{ portable ? '下载与更新节点' : '程序升级' }}</strong>
                        <div v-if="portable" class="node-actions">
                            <el-button size="small" plain :loading="nodeProbing" @click="probeNodes">测速</el-button>
                        </div>
                    </div>
                </template>
                <div v-if="portable" class="node-block">
                    <div class="node-desc">
                        检查更新时两个节点并发拉版本清单，自动用最快的那一个；下载安装包按下面选的节点走，失败会自动换下一个节点继续。
                    </div>
                    <div class="node-grid">
                        <div
                            v-for="n in nodeRows"
                            :key="n.key"
                            class="node-card"
                            :class="{ 'is-current': n.key === nodeForm }"
                            @click="nodeForm = n.key"
                        >
                            <div class="node-card-head">
                                <span class="node-card-name">{{ n.label }}</span>
                                <span v-if="n.key === nodeForm" class="node-card-flag">已选</span>
                            </div>
                            <div class="node-card-lat" :class="{ 'is-bad': n.ok === false, 'is-idle': n.latency == null && n.ok == null }">
                                <template v-if="n.latency != null">{{ n.latency }}<span class="node-card-unit"> ms</span></template>
                                <template v-else-if="n.ok === false">{{ n.error || '不可用' }}</template>
                                <template v-else>未测速</template>
                            </div>
                            <div class="node-card-hint">{{ n.key === nodeForm ? '下载走这个节点' : '点击选用' }}</div>
                        </div>
                    </div>
                    <div class="node-save">
                        <el-button type="primary" :loading="nodeSaving" @click="saveNode">保存节点</el-button>
                        <span class="node-tip">改完立即生效，下次检查更新就用新节点。</span>
                    </div>
                </div>
                <div v-else class="node-alt">
                    <div class="node-alt-title">当前是容器部署，升级走镜像，不走这个下载节点</div>
                    <div class="node-alt-desc">
                        程序内置的「下载安装包 → 替换文件 → 自动重启」只对 Windows 便携版有效。容器里代码在镜像内，替换不了自己，所以这组节点选择在容器版没有作用，已经收起来了。
                    </div>
                    <div class="node-alt-code">docker pull xinya585/ptbrush:latest</div>
                    <div class="node-alt-code">docker compose up -d</div>
                    <div class="node-alt-desc">
                        检查更新在容器里也不会自动跑；想看有没有新版本，比一下本页顶部版本号和官网 <a href="https://zta.cc.cd/changelog.html" target="_blank" rel="noopener">更新日志</a> 即可。三条下载线路是为 Windows 便携版准备的下包镜像。
                    </div>
                </div>
            </el-card>

        </div>
    `,
});
