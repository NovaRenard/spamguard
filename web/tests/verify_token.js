// Проверка токена, выданного Python-ботом (кросс-проверка совместимости): TOKEN, SITE_SECRET в окружении.
const { verifyToken } = require('../api/_lib/auth');
console.log(JSON.stringify(verifyToken(process.env.SITE_SECRET, process.env.TOKEN)));
