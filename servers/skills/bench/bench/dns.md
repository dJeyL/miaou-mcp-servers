# Lire un résultat DNS de `bench`

`dns_lookup` et `reverse_dns` résolvent depuis le réseau du serveur `bench`,
pas depuis celui de l'utilisateur : un nom interne peut y résoudre (ou non)
autrement que sur son poste.

- `dns_lookup` rend les adresses dédoublonnées et triées, IPv4 et IPv6 mêlées ;
  l'ordre n'est donc pas celui du résolveur.
- `reverse_dns` ne rend que le nom principal du PTR, sans ses alias.
- Un message « Échec de résolution » n'est pas une panne de l'outil : le nom ou
  l'adresse n'a simplement pas de réponse vue de ce réseau.
