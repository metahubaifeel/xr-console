#!/bin/bash
SSH="sshpass -p 123 ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no xr@10.230.20.13"
$SSH "docker exec ex001_client-robot_perception_ops-1 bash -lc 'source /opt/xr/setup.bash >/dev/null 2>&1; timeout 25 python3 /tmp/hg2.py'" >/dev/null 2>&1
for pair in "head:/tmp/hd_head.jpg" "chest:/tmp/hd_left.jpg" "bev:/tmp/hd_right.jpg"; do
  n=${pair%%:*}; p=${pair##*:}
  $SSH "docker exec ex001_client-robot_perception_ops-1 cat $p" > /tmp/raw_$n.jpg 2>/dev/null
  s=$(stat -c%s /tmp/raw_$n.jpg 2>/dev/null || echo 0)
  if [ "$s" -gt 5000 ]; then convert /tmp/raw_$n.jpg -quality 88 -resize 880x /tmp/v_$n.jpg 2>/dev/null || cp /tmp/raw_$n.jpg /tmp/v_$n.jpg; fi
done
ls -la /tmp/v_head.jpg /tmp/v_chest.jpg /tmp/v_bev.jpg 2>/dev/null | awk '{print $9, $5}'
