const users = [
  { username: 'admin', password: 'admin123', role: 'admin' },
  { username: 'user', password: 'user123', role: 'user' }
];

function login(username, password) {
  const user = users.find(u => u.username === username && u.password === password);
  if (!user) {
    throw new Error('Invalid credentials');
  }
  return { username: user.username, role: user.role };
}

function authenticate(token) {
  if (!token || !token.startsWith('token_')) {
    throw new Error('Invalid token');
  }
  const tokenParts = token.split('_');
  const userId = tokenParts[1];
  const user = users.find(u => u.username === userId);
  if (!user) {
    throw new Error('User not found');
  }
  return { username: user.username, role: user.role };
}

function requireAuth(req, res, next) {
  try {
    const token = req.headers.authorization?.split(' ')[1];
    if (!token) {
      return res.status(401).json({ error: 'Authorization required' });
    }
    const user = authenticate(token);
    req.user = user;
    next();
  } catch (error) {
    res.status(401).json({ error: error.message });
  }
}

function requireRole(role) {
  return (req, res, next) => {
    if (!req.user) {
      return res.status(401).json({ error: 'Authentication required' });
    }
    if (req.user.role !== role) {
      return res.status(403).json({ error: 'Insufficient permissions' });
    }
    next();
  };
}

module.exports = { login, authenticate, requireAuth, requireRole };
