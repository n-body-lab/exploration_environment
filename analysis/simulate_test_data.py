import csv, random, itertools
random.seed(7)
L=[0.0,0.1,0.3,1.0]
S=[];A=[]
for seed in ["151","152","201","101","102"]:
    for l in L:
        sid=f"s{seed}-{l}"
        agents=[]
        for a in range(1,7):
            rb=0 if a==6 else random.randint(3,15)
            lev=random.choice([0,0,0,1,2,2,3]) if l<0.5 else random.choice([0,0,0,0,1,2])
            agents.append((a,rb,lev,lev if random.random()<.7 else min(lev,1)))
            A.append(dict(session_id=sid,seed=seed,leeway=l,agent=f"agent-{a}",ref_budget=rb,level=lev,level_packets_only=agents[-1][3]))
        m=max(x[2] for x in agents); mp=max(x[3] for x in agents)
        S.append(dict(session_id=sid,seed=seed,leeway=l,excluded=1 if (seed=="102" and l==0.3) else 0,exclusion_reason="broker down (SIMULATED)" if (seed=="102" and l==0.3) else "",y_l2=int(m>=2),y_l3=int(m>=3),y_l4=int(m>=4),y_l2_packets_only=int(mp>=2),offers=random.randint(5,40),final_credits_total=random.randint(0,60)))
for name,rows in [("sim_sessions.csv",S),("sim_agents.csv",A)]:
    with open(name,"w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
