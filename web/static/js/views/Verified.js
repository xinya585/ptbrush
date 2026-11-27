import { defineComponent } from 'vue';

/**
 * 详单页。
 *
 * 把「已实测适配」的站点摊开给用户看：站点、域名、列表页、实测到的免费 / 2X免费 数量、
 * 判定方式（列表 + 详情页双校验）。下面再说明覆盖有限，欢迎提交测试账号做适配，并给出联系方式。
 *
 * 页面是静态数据：站点适配情况由人工实测后写在这里，不依赖后端接口，
 * 避免每次打开都要跑一遍抓取。
 */

const SITES = [
    { name: 'TTG', domain: 'totheglory.im', type: 'NexusPHP', list: 'browse.php', free: 9, free2x: 0, note: 'browse 行含多个外链，已按行首精确切分' },
    { name: '天空', domain: 'hdsky.me', type: 'NexusPHP', list: 'torrents.php', free: 12, free2x: 0, note: '详情页含「同片其他版本」表格，只认本种标题区' },
    { name: '家园', domain: 'hdhome.org', type: 'NexusPHP', list: 'torrents.php', free: 10, free2x: 0, note: '标准标记，列表与详情一致' },
    { name: '彩虹岛', domain: 'ptchdbits.co', type: 'NexusPHP', list: 'torrents.php', free: 7, free2x: 0, note: 'H&R 走详情页判定' },
    { name: '憨憨', domain: 'hhanclub.net', type: 'NexusPHP', list: 'torrents.php', free: 69, free2x: 0, note: 'Tailwind 版式，促销标签在标题后' },
    { name: '我堡', domain: 'ourbits.club', type: 'NexusPHP', list: 'torrents.php', free: 7, free2x: 0, note: '顶部「热门海报」模块已排除' },
    { name: '春天', domain: 'springsunday.net', type: 'NexusPHP', list: 'torrents.php', free: 27, free2x: 0, note: '1/3/14 日返还种不计入免费，按口径跳过' },
    { name: '朋友', domain: 'pt.keepfrds.com', type: 'NexusPHP', list: 'torrents.php', free: 2, free2x: 0, note: '标题栏 [免费] 与页面别处 2X 图标分别判定' },
    { name: '猫站', domain: 'pterclub.net', type: 'NexusPHP', list: 'torrents.php', free: 59, free2x: 1, note: '标准标记，含 2X免费' },
    { name: '皇后', domain: 'open.cd', type: 'NexusPHP', list: 'torrents.php', free: 9, free2x: 4, note: '音乐站，2X免费 与 免费 分开认' },
    { name: '观众', domain: 'audiences.me', type: 'NexusPHP', list: 'torrents.php', free: 78, free2x: 0, note: '无 spstate 筛选器，靠正文标记识别' },
    { name: '馒头', domain: 'zp.m-team.io', type: 'M-Team', list: 'api/search', free: 17, free2x: 0, note: '按 API discount 字段判定' },
    { name: '高清杜比', domain: 'uhddolby.com', type: 'NexusPHP', list: 'torrents.php', free: 73, free2x: 2, note: '标准标记，含 2X免费' },
];

export default defineComponent({
    name: 'Verified',
    setup() {
        // 域名打码：每一段只留开头两个字符，后面全用 * 补齐，TLD 原样保留
        // totheglory.im -> to********.im / pt.keepfrds.com -> pt.******.com / open.cd -> op**.cd
        function maskDomain(domain) {
            const raw = String(domain || '').trim();
            if (!raw) return '';
            const parts = raw.split('.');
            if (parts.length < 2) return raw;
            return parts
                .map((seg, i) => {
                    if (i === parts.length - 1) return seg; // TLD 不动
                    if (seg.length <= 2) return seg;       // 两字符以内的段原样（如 pt 子域）
                    return seg.slice(0, 2) + '*'.repeat(Math.max(1, seg.length - 2));
                })
                .join('.');
        }

        return { sites: SITES, maskDomain };
    },
    template: `
        <div class="sites-page">
            <div class="sites-header">
                <div>
                    <div class="sites-title">详单</div>
                </div>
            </div>

            <el-card shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <strong>适配概括</strong>
                </template>
                <div style="line-height:1.95;color:#4b5563;">
                    <p style="margin:0 0 8px;">
                        PTBrush 的免费判定分两层：先在列表页按站点自己的促销标记识别（图标、class、文案各不相同），
                        种子真的下载后，再定期回查详情页确认促销仍然存在；两层都对上，才算「已适配」。
                    </p>
                    <p style="margin:0 0 8px;">
                        下表是实测跑通过的站点，共 {{ sites.length }} 个。每个站点都做过「列表判免费 / 详情判免费」的双向抽样核对，
                        只认 <el-tag size="small" type="success" effect="plain">免费</el-tag>
                        <el-tag size="small" type="success" effect="plain">2X免费</el-tag>，
                        2X、50%、30%、1/3/14 日返还一律不抓。
                    </p>
                    <p style="margin:0;">
                        实测数字是核对当天的首页真实结果，会随站点发种变化，不代表固定配额。
                    </p>
                </div>
            </el-card>

            <el-card shadow="hover" style="margin-bottom:16px;">
                <template #header>
                    <div class="card-header-row">
                        <strong>已适配站点</strong>
                        <span style="color:#909399;font-size:13px;">共 {{ sites.length }} 个</span>
                    </div>
                </template>
                <el-table :data="sites" size="small" stripe style="width:100%;" class="verified-table">
                    <el-table-column label="站点" min-width="230">
                        <template #default="{ row }">
                            <div class="verified-site-name">{{ row.name }}</div>
                            <div class="verified-site-note">{{ row.note }}</div>
                        </template>
                    </el-table-column>
                    <el-table-column label="域名" min-width="165">
                        <template #default="{ row }">
                            <span class="verified-domain">{{ maskDomain(row.domain) }}</span>
                        </template>
                    </el-table-column>
                    <el-table-column label="类型 / 列表页" min-width="150">
                        <template #default="{ row }">
                            <div>{{ row.type }}</div>
                            <div class="verified-site-note">{{ row.list }}</div>
                        </template>
                    </el-table-column>
                    <el-table-column label="实测免费" width="90" align="center">
                        <template #default="{ row }">{{ row.free }}</template>
                    </el-table-column>
                    <el-table-column label="实测 2X免费" width="105" align="center">
                        <template #default="{ row }">{{ row.free2x }}</template>
                    </el-table-column>
                    <el-table-column label="适配状态" width="95" align="center">
                        <template #default>
                            <el-tag size="small" type="success" effect="plain">已适配</el-tag>
                        </template>
                    </el-table-column>
                </el-table>
                <div class="verified-cards">
                    <div class="verified-card" v-for="row in sites" :key="'m-' + row.domain">
                        <div class="verified-card-head">
                            <div class="verified-card-title">
                                <div class="verified-site-name">{{ row.name }}</div>
                                <div class="verified-card-note">{{ row.note }}</div>
                            </div>
                            <el-tag size="small" type="success" effect="plain">已适配</el-tag>
                        </div>
                        <div class="verified-card-meta">
                            <span class="verified-domain">{{ maskDomain(row.domain) }}</span>
                            <span>{{ row.type }}</span>
                            <span class="verified-site-note">{{ row.list }}</span>
                        </div>
                        <div class="verified-card-stats">
                            <span>实测免费 {{ row.free }}</span>
                            <span>实测 2X免费 {{ row.free2x }}</span>
                        </div>
                    </div>
                </div>
                <div class="field-help" style="margin-top:12px;">
                    海豚（{{ maskDomain('dicmusic.com') }}）本轮没有参与核对，暂未列入。
                </div>
            </el-card>

            <el-card shadow="hover">
                <template #header>
                    <strong>反馈与联系</strong>
                </template>
                <div style="line-height:1.95;color:#4b5563;">
                    <p style="margin:0 0 8px;color:#1f2937;font-weight:600;">站点适配 / 抓取对不上</p>
                    <p style="margin:0 0 10px;">
                        一个人手上的站有限，做不到每个站都天生匹配。如果你的站没出现在上表，
                        或者跑起来发现某一站「该抓的没抓到、不该抓的抓进来了」，把测试账号发给我，
                        我用真实页面抓一次、把标记位置对上，适配好之后会补进这张表。
                    </p>
                    <p style="margin:0 0 10px;">
                        需要提供：站点地址 + 账号密码（或者一份能用的 cookie）。只用于本地抓取测试，
                        不会写进软件的默认配置，也不会拿去做别的事。
                    </p>
                    <div class="verified-contact">
                        <div class="verified-contact-item">
                            <span class="verified-contact-label">邮箱</span>
                            <a href="mailto:scyink@gmail.com">scyink@gmail.com</a>
                        </div>
                        <div class="verified-contact-item">
                            <span class="verified-contact-label">电报群</span>
                            <a href="https://t.me/+sfl6a3I76h45ZWQ1" target="_blank" rel="noopener">加入电报群</a>
                        </div>
                    </div>

                    <el-divider style="margin:22px 0 16px;" />

                    <p style="margin:0 0 8px;color:#1f2937;font-weight:600;">官网</p>
                    <p style="margin:0 0 10px;">
                        安装包下载、版本更新日志、Docker 部署文档都在官网上。
                    </p>
                    <div class="verified-contact">
                        <a href="https://zta.cc.cd/" target="_blank" rel="noopener">
                            <el-button size="small" type="primary" plain>打开官网</el-button>
                        </a>
                        <a href="https://zta.cc.cd/download.html" target="_blank" rel="noopener">
                            <el-button size="small" plain>下载 / 更新</el-button>
                        </a>
                    </div>
                </div>
            </el-card>
        </div>
    `,
});
