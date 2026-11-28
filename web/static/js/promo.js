// 促销类型唯一口径：界面只显示中文词，内部仍用 free / free_2x 等代码。
export const PROMOTION_LABELS = {
    free: '免费',
    free_2x: '2X免费',
    '2x': '2X上传',
    half_down: '50%下载',
    all: '不限制',
};

const PROMOTION_ALIASES = {
    free: 'free',
    freeleech: 'free',
    '免费': 'free',
    '免费下载': 'free',
    free_2x: 'free_2x',
    free2x: 'free_2x',
    '2xfree': 'free_2x',
    '2x_free': 'free_2x',
    '2x免费': 'free_2x',
    '2x 免费': 'free_2x',
    '免费2x': 'free_2x',
    '免费 2x': 'free_2x',
    '双倍免费': 'free_2x',
    '2x': '2x',
    '2x上传': '2x',
    '2x 上传': '2x',
    '2倍': '2x',
    '双倍': '2x',
    half_down: 'half_down',
    halfdown: 'half_down',
    '50%': 'half_down',
    '50%下载': 'half_down',
    '50% 下载': 'half_down',
    '半价': 'half_down',
    all: 'all',
    '不限制': 'all',
    none: 'none',
};

export function normalizePromotion(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    return PROMOTION_ALIASES[raw.toLowerCase()] || raw;
}

export function promotionLabel(value) {
    const key = normalizePromotion(value);
    if (!key) return '';
    return PROMOTION_LABELS[key] || key;
}

export function promotionText(value) {
    if (Array.isArray(value)) {
        return value.map(promotionLabel).filter(Boolean).join('、');
    }
    const parts = String(value || '').split(/[,，/、]/).map(s => s.trim()).filter(Boolean);
    return parts.map(promotionLabel).filter(Boolean).join('、');
}
