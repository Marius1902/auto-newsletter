# Newsletter autonome

Pipeline quotidien : flux RSS → rédaction par Claude → site GitHub Pages + email Buttondown, sans intervention.

## Mise en route (≈ 30 min)
1. Crée un dépôt GitHub, pousse ce dossier.
2. Settings → Pages → source : branche `main`, dossier `/docs`.
3. Settings → Secrets → ajoute `ANTHROPIC_API_KEY` et (optionnel) `BUTTONDOWN_API_KEY`.
4. Édite `config.yaml` : niche, flux RSS, `site_url`, liens affiliés.
5. Onglet Actions → « Édition quotidienne » → Run workflow. Ensuite, tout est automatique.

Test local : `pip install -r requirements.txt && ANTHROPIC_API_KEY=... python main.py`

## Revenus possibles
- **Affiliation** : programmes de ta niche (outils SaaS, formations). Ajoute-les dans `config.yaml`.
- **Sponsoring** : à partir de quelques centaines d'abonnés, vends l'emplacement (`sponsor`).
- **Version payante** : Buttondown permet un tier payant (analyses approfondies, archives).

## Points d'attention
- Les revenus ne sont pas garantis : l'audience se construit (réseaux, SEO, partages), c'est le vrai travail.
- `auto_send: false` par défaut : relis le brouillon quelques semaines avant d'automatiser l'envoi.
- Mentionne les liens affiliés (obligation de transparence, déjà dans le code) et respecte le RGPD pour les abonnés.
- Les revenus doivent être déclarés (micro-entreprise en France).
- Coût : quelques centimes d'API par jour, hébergement gratuit.
